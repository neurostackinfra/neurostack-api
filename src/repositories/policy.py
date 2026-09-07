"""
NeuroStack - Policy Repository
Data access layer for Policy model.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.models.policy import Policy
from src.repositories.base import BaseRepository

logger = get_logger(__name__)


class PolicyRepository(BaseRepository[Policy]):
    """
    Repository for Policy model operations.

    Provides methods for:
    - CRUD operations (inherited from BaseRepository)
    - Finding policies by tenant
    - Finding active policies
    - Finding global policies
    - Finding policies by type
    """

    def __init__(self, session: AsyncSession):
        super().__init__(session, Policy)

    async def find_by_tenant(
        self,
        tenant_id: UUID,
        active_only: bool = True,
        policy_type: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Policy]:
        """
        Find policies for a tenant.

        Args:
            tenant_id: Tenant ID
            active_only: Only return active policies
            policy_type: Optional policy type filter
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of policy instances (ordered by priority DESC)
        """
        filters: dict[str, object] = {"tenant_id": tenant_id}
        if active_only:
            filters["is_active"] = True
        if policy_type:
            filters["policy_type"] = policy_type

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-priority",
            **filters,
        )

    async def find_global(
        self,
        active_only: bool = True,
        policy_type: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Policy]:
        """
        Find global policies (tenant_id is NULL).

        Args:
            active_only: Only return active policies
            policy_type: Optional policy type filter
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of global policy instances (ordered by priority DESC)
        """
        stmt = select(Policy).where(Policy.tenant_id.is_(None))

        if active_only:
            stmt = stmt.where(Policy.is_active.is_(True))
        if policy_type:
            stmt = stmt.where(Policy.policy_type == policy_type)

        stmt = stmt.order_by(Policy.priority.desc())

        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_active(
        self,
        tenant_id: Optional[UUID] = None,
        policy_type: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Policy]:
        """
        Find active policies (tenant-specific or global).

        Args:
            tenant_id: Optional tenant ID filter
            policy_type: Optional policy type filter
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of active policy instances (ordered by priority DESC)
        """
        stmt = select(Policy).where(Policy.is_active.is_(True))

        if tenant_id:
            stmt = stmt.where(
                or_(
                    Policy.tenant_id == tenant_id,
                    Policy.tenant_id.is_(None),  # Include global policies
                )
            )
        if policy_type:
            stmt = stmt.where(Policy.policy_type == policy_type)

        stmt = stmt.order_by(Policy.priority.desc())

        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_type(
        self,
        policy_type: str,
        tenant_id: Optional[UUID] = None,
        active_only: bool = True,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Policy]:
        """
        Find policies by type.

        Args:
            policy_type: Policy type
            tenant_id: Optional tenant ID filter
            active_only: Only return active policies
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of policy instances (ordered by priority DESC)
        """
        filters: dict[str, object] = {"policy_type": policy_type}
        if active_only:
            filters["is_active"] = True
        if tenant_id:
            filters["tenant_id"] = tenant_id

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-priority",
            **filters,
        )

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        active_only: bool = False,
    ) -> int:
        """
        Count policies for a tenant.

        Args:
            tenant_id: Tenant ID
            active_only: Only count active policies

        Returns:
            Number of policies
        """
        stmt = (
            select(func.count())
            .select_from(Policy)
            .where(Policy.tenant_id == tenant_id)
        )
        if active_only:
            stmt = stmt.where(Policy.is_active.is_(True))

        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def soft_delete(self, policy_id: UUID, tenant_id: UUID) -> bool:
        """
        Soft delete a policy (mark as inactive).

        Args:
            policy_id: Policy ID
            tenant_id: Tenant ID (for authorization)

        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(policy_id)
        if instance is None or instance.tenant_id != tenant_id:
            return False

        instance.is_active = False
        instance.increment_version()
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug("Soft deleted policy", policy_id=str(policy_id))
        return True
