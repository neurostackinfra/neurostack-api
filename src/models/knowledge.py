"""
NeuroStack - Knowledge Document Model
Represents documents in the RAG knowledge base.
"""

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, Text, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.tenant import Tenant


class KnowledgeDocument(Base, TimestampMixin):
    """
    Knowledge document model for RAG retrieval.

    Stores documents that can be queried semantically to provide
    context-aware responses. Documents are:
    - Indexed in a vector database (Qdrant)
    - Associated with a specific tenant
    - Searchable by semantic similarity

    Attributes:
        id: Unique document identifier (UUID)
        tenant_id: Owning tenant
        title: Document title
        content: Full document content
        content_type: Type of content (text, html, markdown, etc.)
        metadata: Additional metadata (tags, category, source, etc.)
        vector_id: Reference to vector in Qdrant
        is_indexed: Whether document has been indexed
        chunk_count: Number of chunks document was split into
        created_at: Creation timestamp
        updated_at: Last update timestamp

    Relationships:
        tenant: The tenant that owns this document
    """

    __tablename__ = "knowledge_documents"

    # Primary Key
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Foreign Key
    tenant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Core Fields
    title: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    content_type: Mapped[str] = mapped_column(
        String(50),
        default="text",
        nullable=False,
    )

    # Document Metadata (JSONB)
    # Note: Named document_metadata to avoid conflict with SQLAlchemy's reserved 'metadata'
    document_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata",  # Column name in DB
        JSONB,
        default=dict,
        nullable=False,
    )

    # Vector DB Reference
    vector_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    # Indexing Status
    is_indexed: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
    )
    chunk_count: Mapped[int] = mapped_column(
        default=0,
        nullable=False,
    )

    # Logical size of the original RAG payload. Persist bytes instead of a
    # derived MB float so billing calculations remain exact and inexpensive.
    size_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )

    # Error tracking
    index_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Relationship
    tenant: Mapped["Tenant"] = relationship(
        "Tenant",
        back_populates="knowledge_documents",
    )

    @property
    def category(self) -> Optional[str]:
        """Get document category from metadata."""
        metadata = self.document_metadata
        if not isinstance(metadata, dict):
            return None
        category = metadata.get("category")
        return category if isinstance(category, str) else None

    @property
    def tags(self) -> list[str]:
        """Get document tags from metadata."""
        metadata = self.document_metadata
        if not isinstance(metadata, dict):
            return []
        tags = metadata.get("tags", [])
        if not isinstance(tags, list):
            return []
        return [tag for tag in tags if isinstance(tag, str)]

    @property
    def source(self) -> Optional[str]:
        """Get document source from metadata."""
        metadata = self.document_metadata
        if not isinstance(metadata, dict):
            return None
        source = metadata.get("source")
        return source if isinstance(source, str) else None

    def mark_indexed(self, vector_id: str, chunk_count: int = 1) -> None:
        """Mark document as successfully indexed."""
        self.is_indexed = True
        self.vector_id = vector_id
        self.chunk_count = chunk_count
        self.index_error = None

    def mark_index_failed(self, error: str) -> None:
        """Mark document indexing as failed."""
        self.is_indexed = False
        self.index_error = error

    def add_tag(self, tag: str) -> None:
        """Add a tag to document metadata."""
        current_metadata = self.document_metadata
        metadata = dict(current_metadata) if isinstance(current_metadata, dict) else {}
        current_tags = metadata.get("tags")
        tags = list(current_tags) if isinstance(current_tags, list) else []
        if tag not in tags:
            tags.append(tag)
            metadata["tags"] = tags
            self.document_metadata = metadata

    def remove_tag(self, tag: str) -> bool:
        """Remove a tag from document metadata. Returns True if removed."""
        current_metadata = self.document_metadata
        if not isinstance(current_metadata, dict):
            return False
        current_tags = current_metadata.get("tags")
        tags = list(current_tags) if isinstance(current_tags, list) else []
        if tag in tags:
            tags.remove(tag)
            metadata = dict(current_metadata)
            metadata["tags"] = tags
            self.document_metadata = metadata
            return True
        return False

    def set_category(self, category: str) -> None:
        """Set document category in metadata."""
        current_metadata = self.document_metadata
        metadata = dict(current_metadata) if isinstance(current_metadata, dict) else {}
        metadata["category"] = category
        self.document_metadata = metadata

    @property
    def content_preview(self) -> str:
        """Get first 200 characters of content."""
        if len(self.content) <= 200:
            return self.content
        return self.content[:200] + "..."

    def __repr__(self) -> str:
        indexed = "indexed" if self.is_indexed else "not indexed"
        return f"<KnowledgeDocument(id={self.id}, title='{self.title[:30]}...', {indexed})>"


@event.listens_for(KnowledgeDocument, "before_insert")
@event.listens_for(KnowledgeDocument, "before_update")
def _sync_knowledge_document_size(
    _mapper: Mapper[KnowledgeDocument],
    _connection: Connection,
    target: KnowledgeDocument,
) -> None:
    """Keep the persisted logical size aligned with UTF-8 document content."""
    target.size_bytes = len((target.content or "").encode("utf-8"))
