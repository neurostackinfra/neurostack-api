"""
NeuroStack - Tenant Repository
Data access layer for Tenant model.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config.logging import get_logger
from src.models.clinic_config import ClinicConfig
from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.repositories.base import BaseRepository

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

    async def find_by_api_key_hash_with_clinic_config(
        self, api_key_hash: str
    ) -> Optional[Tenant]:
        """
        Find tenant by API key hash com clinic_config carregado (tabela clinic_configs).
        """
        stmt = (
            select(Tenant)
            .where(Tenant.api_key_hash == api_key_hash)
            .options(selectinload(Tenant.clinic_config_row))
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id_with_clinic_config(self, tenant_id: UUID) -> Optional[Tenant]:
        """
        Busca tenant por ID com clinic_config carregado (tabela clinic_configs).
        """
        return await self.get_by_id(
            tenant_id,
            load_relationships=["clinic_config_row"],
        )

    async def upsert_clinic_config(
        self, tenant_id: UUID, clinic_config: ClinicConfig
    ) -> ClinicConfigTable:
        """
        Cria ou atualiza a configuração da clínica do tenant (tabela clinic_configs).
        """
        stmt = select(ClinicConfigTable).where(ClinicConfigTable.tenant_id == tenant_id)
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        if row:
            row.clinic_id = clinic_config.clinic_id
            row.version = clinic_config.version
            row.persona = clinic_config.persona.model_dump()
            row.scheduling = clinic_config.scheduling.model_dump()
            row.pricing = clinic_config.pricing.model_dump()
            row.post_care = clinic_config.post_care.model_dump()
            row.handoff = clinic_config.handoff.model_dump()
            await self.session.flush()
            await self.session.refresh(row)
            return row
        entity = ClinicConfigTable.from_pydantic(tenant_id, clinic_config)
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def find_active(
        self, skip: int = 0, limit: Optional[int] = None
    ) -> list[Tenant]:
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
        stmt = (
            select(func.count()).select_from(Tenant).where(Tenant.is_active.is_(True))
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0
