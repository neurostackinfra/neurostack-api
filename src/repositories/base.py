"""
NeuroStack - Base Repository
Abstract base class for all repositories implementing common CRUD operations.
"""

from typing import Any, Generic, Optional, TypeVar
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config.logging import get_logger
from src.models.base import Base

logger = get_logger(__name__)

# Type variable for model classes
ModelType = TypeVar("ModelType", bound=Base)


class BaseRepository(Generic[ModelType]):
    """
    Base repository providing common CRUD operations.

    All repositories should inherit from this class and provide
    the model class as the generic type parameter.

    Example:
        class TenantRepository(BaseRepository[Tenant]):
            def __init__(self, session: AsyncSession):
                super().__init__(session, Tenant)
    """

    def __init__(self, session: AsyncSession, model: type[ModelType]):
        """
        Initialize repository.

        Args:
            session: Database session
            model: SQLAlchemy model class
        """
        self.session = session
        self.model = model

    # ============================================
    # Create Operations
    # ============================================

    async def create(self, **kwargs: object) -> ModelType:
        """
        Create a new record.

        Args:
            **kwargs: Model field values

        Returns:
            Created model instance
        """
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug(f"Created {self.model.__name__}", id=str(getattr(instance, "id")))
        return instance

    async def create_many(self, items: list[dict[str, Any]]) -> list[ModelType]:
        """
        Create multiple records in a single operation.

        Args:
            items: List of dictionaries with field values

        Returns:
            List of created model instances
        """
        instances = [self.model(**item) for item in items]
        self.session.add_all(instances)
        await self.session.flush()
        for instance in instances:
            await self.session.refresh(instance)
        logger.debug(f"Created {len(instances)} {self.model.__name__} records")
        return instances

    # ============================================
    # Read Operations
    # ============================================

    async def get_by_id(
        self, id: UUID, load_relationships: Optional[list[str]] = None
    ) -> Optional[ModelType]:
        """
        Get a record by ID.

        Args:
            id: Record ID
            load_relationships: Optional list of relationship names to eager load

        Returns:
            Model instance or None if not found
        """
        stmt = select(self.model).where(getattr(self.model, "id") == id)

        if load_relationships:
            for rel in load_relationships:
                stmt = stmt.options(selectinload(getattr(self.model, rel)))

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all(
        self,
        skip: int = 0,
        limit: Optional[int] = None,
        load_relationships: Optional[list[str]] = None,
    ) -> list[ModelType]:
        """
        Get all records with pagination.

        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return
            load_relationships: Optional list of relationship names to eager load

        Returns:
            List of model instances
        """
        stmt = select(self.model)

        if load_relationships:
            for rel in load_relationships:
                stmt = stmt.options(selectinload(getattr(self.model, rel)))

        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count(self) -> int:
        """
        Count total records.

        Returns:
            Total count
        """
        stmt = select(func.count()).select_from(self.model)
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def exists(self, id: UUID) -> bool:
        """
        Check if a record exists by ID.

        Args:
            id: Record ID

        Returns:
            True if exists, False otherwise
        """
        instance = await self.get_by_id(id)
        return instance is not None

    # ============================================
    # Update Operations
    # ============================================

    async def update(self, id: UUID, **kwargs: object) -> Optional[ModelType]:
        """
        Update a record by ID.

        Args:
            id: Record ID
            **kwargs: Fields to update

        Returns:
            Updated model instance or None if not found
        """
        instance = await self.get_by_id(id)
        if instance is None:
            return None

        for key, value in kwargs.items():
            if hasattr(instance, key):
                setattr(instance, key, value)

        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug(f"Updated {self.model.__name__}", id=str(id))
        return instance

    async def update_many(
        self, filter_criteria: dict[str, object], **kwargs: object
    ) -> int:
        """
        Update multiple records matching criteria.

        Args:
            filter_criteria: Criteria to match records
            **kwargs: Fields to update

        Returns:
            Number of records updated
        """
        stmt = (
            update(self.model)
            .where(*[getattr(self.model, k) == v for k, v in filter_criteria.items()])
            .values(**kwargs)
        )

        result = await self.session.execute(stmt)
        await self.session.flush()
        count = getattr(result, "rowcount", None)
        if not isinstance(count, int):
            raise RuntimeError("Database result did not provide an integer rowcount")
        logger.debug(f"Updated {count} {self.model.__name__} records")
        return count

    # ============================================
    # Delete Operations
    # ============================================

    async def delete(self, id: UUID) -> bool:
        """
        Delete a record by ID.

        Args:
            id: Record ID

        Returns:
            True if deleted, False if not found
        """
        instance = await self.get_by_id(id)
        if instance is None:
            return False

        await self.session.delete(instance)
        await self.session.flush()
        logger.debug(f"Deleted {self.model.__name__}", id=str(id))
        return True

    async def delete_many(self, filter_criteria: dict[str, object]) -> int:
        """
        Delete multiple records matching criteria.

        Args:
            filter_criteria: Criteria to match records

        Returns:
            Number of records deleted
        """
        stmt = delete(self.model).where(
            *[getattr(self.model, k) == v for k, v in filter_criteria.items()]
        )

        result = await self.session.execute(stmt)
        await self.session.flush()
        count = getattr(result, "rowcount", None)
        if not isinstance(count, int):
            raise RuntimeError("Database result did not provide an integer rowcount")
        logger.debug(f"Deleted {count} {self.model.__name__} records")
        return count

    # ============================================
    # Query Helpers
    # ============================================

    async def find_one(self, **filters: object) -> Optional[ModelType]:
        """
        Find a single record matching filters.

        Args:
            **filters: Field filters (e.g., name="value")

        Returns:
            Model instance or None if not found
        """
        stmt = select(self.model)
        for key, value in filters.items():
            if hasattr(self.model, key):
                stmt = stmt.where(getattr(self.model, key) == value)

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def find_many(
        self,
        skip: int = 0,
        limit: Optional[int] = None,
        order_by: Optional[str] = None,
        **filters: object,
    ) -> list[ModelType]:
        """
        Find multiple records matching filters.

        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return
            order_by: Field name to order by (use '-' prefix for DESC)
            **filters: Field filters

        Returns:
            List of model instances
        """
        stmt = select(self.model)

        # Apply filters
        for key, value in filters.items():
            if hasattr(self.model, key):
                stmt = stmt.where(getattr(self.model, key) == value)

        # Apply ordering
        if order_by:
            if order_by.startswith("-"):
                field = getattr(self.model, order_by[1:])
                stmt = stmt.order_by(field.desc())
            else:
                field = getattr(self.model, order_by)
                stmt = stmt.order_by(field.asc())

        # Apply pagination
        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())
