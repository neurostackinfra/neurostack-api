#!/usr/bin/env python3
"""
Script para criar tenant de desenvolvimento no banco de dados.
Execute este script se o tenant de desenvolvimento não existir.
"""

import asyncio
import sys
from pathlib import Path
from uuid import UUID

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from src.config.settings import settings
from src.models.tenant import TenantConfig, Tenant
from src.repositories import TenantRepository
import bcrypt


async def create_dev_tenant():
    """Create development tenant if it doesn't exist."""
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    async with session_factory() as session:
        repo = TenantRepository(session)
        
        tenant_id = UUID("a0000000-0000-0000-0000-000000000001")
        
        # Check if tenant exists
        existing = await repo.get_by_id(tenant_id)
        
        if existing:
            print(f"✅ Tenant {tenant_id} already exists: {existing.name}")
            return
        
        # Create API key hash for 'test-api-key'
        api_key = "test-api-key"
        api_key_hash = bcrypt.hashpw(api_key.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        
        # Create tenant config
        config = TenantConfig(
            description="Default development tenant",
            rate_limit=100,
            session_ttl_seconds=86400,
            max_sessions_per_user=10,
            features={
                "rag_enabled": True,
                "handoff_enabled": True,
                "safety_strict_mode": False,
            },
        )
        
        # Create tenant with config
        tenant = await repo.create(
            id=tenant_id,
            name="Development Tenant",
            slug="dev-tenant",
            api_key_hash=api_key_hash,
            is_active=True,
            config=config.to_dict(),
        )
        
        await session.commit()
        
        print(f"✅ Created development tenant:")
        print(f"   ID: {tenant.id}")
        print(f"   Name: {tenant.name}")
        print(f"   API Key: {api_key}")
        print(f"   Slug: {tenant.slug}")


if __name__ == "__main__":
    asyncio.run(create_dev_tenant())
