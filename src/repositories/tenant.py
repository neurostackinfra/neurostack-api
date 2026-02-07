"""
NeuroStack - Tenant Repository
Data access layer for Tenant model.
"""

from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.tenant import Tenant
from src.repositories.base import BaseRepository
from src.config.logging import get_logger

logger = get_logger(__name__)


class TenantRepository(BaseRepository[Tenant]):
    """
    Repository for Tenant model operations.
    
    Provides methods for:
    - CRUD operations (inherited from BaseRepository)
    - Finding tenants by slug
    - Finding tenants by API key hash
    - Listing active tenants
    """
    
    def __init__(self, session: AsyncSession):
        super().__init__(session, Tenant)
    
    async def find_by_slug(self, slug: str) -> Optional[Tenant]:
        """
        Find tenant by slug.
        
        Args:
            slug: Tenant slug
            
        Returns:
            Tenant instance or None if not found
        """
        stmt = select(Tenant).where(Tenant.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
    
    async def find_by_api_key_hash(self, api_key_hash: str) -> Optional[Tenant]:
        """
        Find tenant by API key hash.
        
        Args:
            api_key_hash: Hashed API key
            
        Returns:
            Tenant instance or None if not found
        """
        stmt = select(Tenant).where(Tenant.api_key_hash == api_key_hash)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
    
    async def find_active(self, skip: int = 0, limit: Optional[int] = None) -> list[Tenant]:
        """
        Find all active tenants.
        
        Args:
            skip: Number of records to skip
            limit: Maximum number of records to return
            
        Returns:
            List of active tenant instances
        """
        return await self.find_many(
            skip=skip,
            limit=limit,
            is_active=True,
            order_by="-created_at",
        )
    
    async def count_active(self) -> int:
        """
        Count active tenants.
        
        Returns:
            Number of active tenants
        """
        stmt = select(func.count()).select_from(Tenant).where(Tenant.is_active == True)
        result = await self.session.execute(stmt)
        return result.scalar() or 0
