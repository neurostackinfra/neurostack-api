"""
NeuroStack API - Knowledge Schemas
Request and response models for knowledge base endpoints.
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentMetadata(BaseModel):
    """Metadata for an uploaded document."""

    title: str | None = Field(default=None, description="Document title")
    source: str | None = Field(default=None, description="Document source")
    category: str | None = Field(default=None, description="Document category")
    tags: list[str] = Field(default_factory=list, description="Tags for filtering")
    custom: dict[str, Any] = Field(default_factory=dict, description="Custom metadata")


class DocumentUploadRequest(BaseModel):
    """Request to upload a document."""

    model_config = {
        "json_schema_extra": {
            "example": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "content": "A clínica funciona de segunda a sexta, das 8h às 18h...",
                "metadata": {
                    "title": "Horários de Atendimento",
                    "category": "info",
                    "tags": ["horarios", "atendimento"],
                },
            }
        }
    }

    tenant_id: UUID = Field(..., description="Tenant that owns this document")
    content: str = Field(
        ...,
        min_length=10,
        max_length=100000,
        description="Document content to index",
    )
    metadata: DocumentMetadata = Field(
        default_factory=DocumentMetadata,
        description="Document metadata",
    )


class DocumentResponse(BaseModel):
    """Response for document operations."""

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "doc-123",
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "status": "indexed",
                "metadata": {"title": "Horários de Atendimento"},
                "chunk_count": 3,
                "created_at": "2025-12-31T10:30:00Z",
            }
        }
    }

    id: str = Field(..., description="Document identifier")
    tenant_id: UUID = Field(..., description="Tenant that owns this document")
    status: str = Field(..., description="Indexing status")
    metadata: DocumentMetadata = Field(..., description="Document metadata")
    chunk_count: int = Field(default=0, description="Number of indexed chunks")
    created_at: str = Field(..., description="Creation timestamp")


class SearchRequest(BaseModel):
    """Request for semantic search."""

    model_config = {
        "json_schema_extra": {
            "example": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "query": "Qual o horário de funcionamento?",
                "top_k": 5,
                "filters": {"category": "info"},
            }
        }
    }

    tenant_id: UUID = Field(..., description="Tenant to search in")
    query: str = Field(
        ...,
        min_length=3,
        max_length=1000,
        description="Search query",
    )
    top_k: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Number of results to return",
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="Metadata filters to apply",
    )
    min_score: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Minimum relevance score",
    )


class SearchResult(BaseModel):
    """A single search result."""

    document_id: str = Field(..., description="Source document ID")
    chunk_id: str = Field(..., description="Chunk identifier")
    content: str = Field(..., description="Matched content")
    score: float = Field(..., ge=0.0, le=1.0, description="Relevance score")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Document metadata")


class SearchResponse(BaseModel):
    """Response for semantic search."""

    model_config = {
        "json_schema_extra": {
            "example": {
                "query": "Qual o horário de funcionamento?",
                "results": [
                    {
                        "document_id": "doc-123",
                        "chunk_id": "chunk-456",
                        "content": "A clínica funciona de segunda a sexta, das 8h às 18h.",
                        "score": 0.95,
                        "metadata": {"title": "Horários de Atendimento"},
                    }
                ],
                "total": 1,
                "processing_time_ms": 25,
            }
        }
    }

    query: str = Field(..., description="Original query")
    results: list[SearchResult] = Field(..., description="Search results")
    total: int = Field(..., description="Total results found")
    processing_time_ms: int = Field(..., description="Processing time in milliseconds")


class DocumentListResponse(BaseModel):
    """Response for document list."""

    documents: list[DocumentResponse] = Field(..., description="List of documents")
    total: int = Field(..., description="Total documents")
    page: int = Field(..., description="Current page")
    page_size: int = Field(..., description="Items per page")


__all__ = [
    "DocumentMetadata",
    "DocumentUploadRequest",
    "DocumentResponse",
    "SearchRequest",
    "SearchResult",
    "SearchResponse",
    "DocumentListResponse",
]
