"""
NeuroStack - Knowledge Document Repository
Data access layer for KnowledgeDocument model.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.models.knowledge import KnowledgeDocument
from src.repositories.base import BaseRepository

logger = get_logger(__name__)


class KnowledgeDocumentRepository(BaseRepository[KnowledgeDocument]):
    """
    Repository for KnowledgeDocument model operations.

    Provides methods for:
    - CRUD operations (inherited from BaseRepository)
    - Finding documents by tenant
    - Finding indexed documents
    - Finding documents by category
    - Finding documents by tags
    """

    def __init__(self, session: AsyncSession):
        super().__init__(session, KnowledgeDocument)

    async def find_by_tenant(
        self,
        tenant_id: UUID,
        indexed_only: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[KnowledgeDocument]:
        """
        Find documents for a tenant.

        Args:
            tenant_id: Tenant ID
            indexed_only: Only return indexed documents
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of document instances (ordered by created_at DESC)
        """
        filters: dict[str, object] = {"tenant_id": tenant_id}
        if indexed_only:
            filters["is_indexed"] = True

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-created_at",
            **filters,
        )

    async def find_indexed(
        self,
        tenant_id: UUID,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[KnowledgeDocument]:
        """
        Find indexed documents for a tenant.

        Args:
            tenant_id: Tenant ID
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of indexed document instances
        """
        return await self.find_by_tenant(
            tenant_id=tenant_id,
            indexed_only=True,
            skip=skip,
            limit=limit,
        )

    async def find_not_indexed(
        self,
        tenant_id: UUID,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[KnowledgeDocument]:
        """
        Find documents that are not yet indexed.

        Args:
            tenant_id: Tenant ID
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of non-indexed document instances
        """
        filters = {
            "tenant_id": tenant_id,
            "is_indexed": False,
        }

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="created_at",
            **filters,
        )

    async def find_by_category(
        self,
        tenant_id: UUID,
        category: str,
        indexed_only: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[KnowledgeDocument]:
        """
        Find documents by category.

        Note: This uses JSONB metadata filtering.

        Args:
            tenant_id: Tenant ID
            category: Category name
            indexed_only: Only return indexed documents
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of document instances
        """
        stmt = select(KnowledgeDocument).where(
            and_(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.document_metadata["category"].astext == category,
            )
        )

        if indexed_only:
            stmt = stmt.where(KnowledgeDocument.is_indexed.is_(True))

        stmt = stmt.order_by(KnowledgeDocument.created_at.desc())

        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        indexed_only: bool = False,
    ) -> int:
        """
        Count documents for a tenant.

        Args:
            tenant_id: Tenant ID
            indexed_only: Only count indexed documents

        Returns:
            Number of documents
        """
        stmt = (
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.tenant_id == tenant_id)
        )
        if indexed_only:
            stmt = stmt.where(KnowledgeDocument.is_indexed.is_(True))

        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def mark_indexed(
        self,
        document_id: UUID,
        vector_id: str,
        chunk_count: int = 1,
    ) -> bool:
        """
        Mark a document as indexed.

        Args:
            document_id: Document ID
            vector_id: Vector database ID
            chunk_count: Number of chunks

        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(document_id)
        if instance is None:
            return False

        instance.mark_indexed(vector_id, chunk_count)
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug("Marked document as indexed", document_id=str(document_id))
        return True

    async def mark_index_failed(
        self,
        document_id: UUID,
        error: str,
    ) -> bool:
        """
        Mark document indexing as failed.

        Args:
            document_id: Document ID
            error: Error message

        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(document_id)
        if instance is None:
            return False

        instance.mark_index_failed(error)
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug("Marked document indexing as failed", document_id=str(document_id))
        return True
