"""
NeuroStack - Knowledge Domain Models
Domain models for knowledge base and RAG operations.
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class KnowledgeChunk(BaseModel):
    """A chunk of knowledge from the knowledge base."""

    id: str = Field(..., description="Chunk identifier")
    content: str = Field(..., description="Chunk content")
    source: str = Field(..., description="Source document")
    relevance_score: float = Field(..., ge=0.0, le=1.0, description="Relevance score")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )


class KnowledgeDocument(BaseModel):
    """A document in the knowledge base."""

    id: str = Field(..., description="Document identifier")
    tenant_id: UUID = Field(..., description="Tenant that owns this document")
    title: str = Field(..., description="Document title")
    content: str = Field(..., description="Document content")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Document metadata"
    )


class SearchQuery(BaseModel):
    """Query for knowledge search."""

    tenant_id: UUID = Field(..., description="Tenant ID")
    query: str = Field(..., description="Search query")
    top_k: int = Field(default=5, ge=1, le=20, description="Number of results")
    filters: dict[str, Any] = Field(
        default_factory=dict, description="Metadata filters"
    )
    min_score: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Minimum relevance score"
    )


class SearchResult(BaseModel):
    """Result from knowledge search."""

    chunks: list[KnowledgeChunk] = Field(..., description="Relevant chunks")
    total: int = Field(..., description="Total results found")
    query_time_ms: float = Field(..., description="Query execution time")


__all__ = [
    "KnowledgeChunk",
    "KnowledgeDocument",
    "SearchQuery",
    "SearchResult",
]
