"""
NeuroStack API - Knowledge Schemas
Request and response models for knowledge base endpoints.

Upload input is validated here, before it reaches PostgreSQL: ``text`` and
``jsonb`` cannot hold NUL (U+0000) or an unpaired UTF-16 surrogate, ``jsonb``
rejects ``NaN``/``Infinity``, and ``knowledge_documents.title`` is
``VARCHAR(500)``. Every limit below answers ``422`` instead of a driver error.
The response models stay unconstrained, so documents stored before these
limits existed are still readable.
"""

import json
import math
import re
from typing import Annotated, Any, Optional
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
)
from pydantic_core import PydanticCustomError

# Upload limits (documented in docs/api-contracts.md, "Limites de entrada").
KNOWLEDGE_CONTENT_MIN_LENGTH = 10
KNOWLEDGE_CONTENT_MAX_LENGTH = 100_000
# Mirrors knowledge_documents.title VARCHAR(500) (migration 001, model).
KNOWLEDGE_TITLE_MAX_LENGTH = 500
# source, category and tags live in the jsonb ``metadata`` column, which has
# no length of its own; these bounds keep one document's metadata small.
KNOWLEDGE_SOURCE_MAX_LENGTH = 2048
KNOWLEDGE_CATEGORY_MAX_LENGTH = 100
KNOWLEDGE_TAGS_MAX_ITEMS = 50
KNOWLEDGE_TAG_MAX_LENGTH = 100
# metadata.custom: nesting depth counts ``custom`` itself as level 1, keys are
# counted across every nested object, and the size is compact UTF-8 JSON.
KNOWLEDGE_CUSTOM_MAX_DEPTH = 5
KNOWLEDGE_CUSTOM_MAX_KEYS = 100
KNOWLEDGE_CUSTOM_MAX_BYTES = 10_000
# Top-level keys of the stored metadata owned by DocumentMetadata itself;
# ``custom`` is merged next to them and must never overwrite one.
KNOWLEDGE_RESERVED_METADATA_KEYS = ("title", "source", "category", "tags")

_UNPAIRED_SURROGATE = re.compile("[\ud800-\udfff]")


def _text_error(value: str) -> Optional[PydanticCustomError]:
    """The reason PostgreSQL cannot store ``value``, never quoting it."""
    if "\x00" in value:
        return PydanticCustomError(
            "nul_character", "Text must not contain the NUL character (U+0000)"
        )
    if _UNPAIRED_SURROGATE.search(value):
        return PydanticCustomError(
            "unpaired_surrogate",
            "Text must not contain unpaired UTF-16 surrogates (U+D800-U+DFFF)",
        )
    return None


def reject_unstorable_text(value: str) -> str:
    """Pydantic validator: refuse text that PostgreSQL cannot hold."""
    error = _text_error(value)
    if error is not None:
        raise error
    return value


ContentText = Annotated[
    str,
    StringConstraints(
        min_length=KNOWLEDGE_CONTENT_MIN_LENGTH,
        max_length=KNOWLEDGE_CONTENT_MAX_LENGTH,
    ),
    AfterValidator(reject_unstorable_text),
]
TitleText = Annotated[
    str,
    StringConstraints(max_length=KNOWLEDGE_TITLE_MAX_LENGTH),
    AfterValidator(reject_unstorable_text),
]
SourceText = Annotated[
    str,
    StringConstraints(max_length=KNOWLEDGE_SOURCE_MAX_LENGTH),
    AfterValidator(reject_unstorable_text),
]
CategoryText = Annotated[
    str,
    StringConstraints(max_length=KNOWLEDGE_CATEGORY_MAX_LENGTH),
    AfterValidator(reject_unstorable_text),
]
TagText = Annotated[
    str,
    StringConstraints(max_length=KNOWLEDGE_TAG_MAX_LENGTH),
    AfterValidator(reject_unstorable_text),
]


def _custom_error(custom: dict[str, Any]) -> Optional[PydanticCustomError]:
    """First reason ``custom`` cannot be stored, never quoting its content.

    Walks iteratively and stops at the depth limit, so a hostile nesting
    costs no recursion and no more than the limit of work per branch.
    """
    reserved = [key for key in KNOWLEDGE_RESERVED_METADATA_KEYS if key in custom]
    if reserved:
        return PydanticCustomError(
            "reserved_metadata_key",
            "metadata.custom must not use the reserved keys: {keys}",
            {"keys": ", ".join(reserved)},
        )
    keys = 0
    stack: list[tuple[Any, int]] = [(custom, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            if depth > KNOWLEDGE_CUSTOM_MAX_DEPTH:
                return PydanticCustomError(
                    "metadata_too_deep",
                    "metadata.custom must not nest more than {limit} levels",
                    {"limit": KNOWLEDGE_CUSTOM_MAX_DEPTH},
                )
            if isinstance(value, dict):
                keys += len(value)
                if keys > KNOWLEDGE_CUSTOM_MAX_KEYS:
                    return PydanticCustomError(
                        "metadata_too_many_keys",
                        "metadata.custom must not have more than {limit} keys",
                        {"limit": KNOWLEDGE_CUSTOM_MAX_KEYS},
                    )
                for key, item in value.items():
                    error = _text_error(key)
                    if error is not None:
                        return error
                    stack.append((item, depth + 1))
            else:
                stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, str):
            error = _text_error(value)
            if error is not None:
                return error
        elif isinstance(value, float) and not math.isfinite(value):
            return PydanticCustomError(
                "non_finite_number",
                "metadata.custom numbers must be finite (no NaN or Infinity)",
            )
    size = len(
        json.dumps(custom, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if size > KNOWLEDGE_CUSTOM_MAX_BYTES:
        return PydanticCustomError(
            "metadata_too_large",
            "metadata.custom must not exceed {limit} bytes as JSON",
            {"limit": KNOWLEDGE_CUSTOM_MAX_BYTES},
        )
    return None


class DocumentMetadata(BaseModel):
    """Metadata of a stored document, as returned by the API."""

    title: Optional[str] = Field(default=None, description="Document title")
    source: Optional[str] = Field(default=None, description="Document source")
    category: Optional[str] = Field(default=None, description="Document category")
    tags: list[str] = Field(default_factory=list, description="Tags for filtering")
    custom: dict[str, Any] = Field(default_factory=dict, description="Custom metadata")


class DocumentMetadataInput(BaseModel):
    """Metadata accepted on upload, bounded to what can be stored."""

    title: Optional[TitleText] = Field(
        default=None,
        description=(
            f"Document title (at most {KNOWLEDGE_TITLE_MAX_LENGTH} characters)"
        ),
    )
    source: Optional[SourceText] = Field(
        default=None,
        description=(
            f"Document source (at most {KNOWLEDGE_SOURCE_MAX_LENGTH} characters)"
        ),
    )
    category: Optional[CategoryText] = Field(
        default=None,
        description=(
            f"Document category (at most {KNOWLEDGE_CATEGORY_MAX_LENGTH} characters)"
        ),
    )
    tags: list[TagText] = Field(
        default_factory=list,
        max_length=KNOWLEDGE_TAGS_MAX_ITEMS,
        description=(
            f"Tags for filtering (at most {KNOWLEDGE_TAGS_MAX_ITEMS} tags of at "
            f"most {KNOWLEDGE_TAG_MAX_LENGTH} characters)"
        ),
    )
    custom: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Custom metadata, stored next to the fields above. It must not use "
            f"the reserved keys {', '.join(KNOWLEDGE_RESERVED_METADATA_KEYS)}; at "
            f"most {KNOWLEDGE_CUSTOM_MAX_DEPTH} nesting levels, "
            f"{KNOWLEDGE_CUSTOM_MAX_KEYS} keys in total and "
            f"{KNOWLEDGE_CUSTOM_MAX_BYTES} bytes as JSON; finite numbers only."
        ),
    )

    @field_validator("custom")
    @classmethod
    def _custom_is_storable(cls, custom: dict[str, Any]) -> dict[str, Any]:
        error = _custom_error(custom)
        if error is not None:
            raise error
        return custom


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

    tenant_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Tenant that will own the document. Defaults to the caller's "
            "tenant; another tenant is allowed only for a builder that owns "
            "the clinic or a super admin."
        ),
    )
    content: ContentText = Field(
        ...,
        description=(
            f"Document content to index ({KNOWLEDGE_CONTENT_MIN_LENGTH} to "
            f"{KNOWLEDGE_CONTENT_MAX_LENGTH} characters)"
        ),
    )
    metadata: DocumentMetadataInput = Field(
        default_factory=DocumentMetadataInput,
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

    tenant_id: Optional[UUID] = Field(
        default=None,
        description=(
            "Tenant to search in. Defaults to the caller's tenant; another "
            "tenant is allowed only for a builder that owns the clinic or a "
            "super admin."
        ),
    )
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
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Document metadata"
    )


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


class KnowledgeValidationIssue(BaseModel):
    """One request validation issue, without FastAPI's ``input`` field."""

    type: str = Field(..., description="Stable error code, e.g. nul_character")
    loc: list[str | int] = Field(..., description="Where the invalid value is")
    msg: str = Field(..., description="Human-readable reason, without the value")


class KnowledgeValidationErrorResponse(BaseModel):
    """HTTP 422 body of the knowledge document routes."""

    detail: list[KnowledgeValidationIssue]


__all__ = [
    "CategoryText",
    "DocumentMetadata",
    "DocumentMetadataInput",
    "DocumentUploadRequest",
    "DocumentResponse",
    "SearchRequest",
    "SearchResult",
    "SearchResponse",
    "DocumentListResponse",
    "KnowledgeValidationIssue",
    "KnowledgeValidationErrorResponse",
]
