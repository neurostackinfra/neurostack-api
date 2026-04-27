#!/usr/bin/env python3
"""
Seed script for the NeuroStack demo environment.

Creates a tenant, users, clinic config, session, and sample events
for testing the admin panel and API endpoints.

Idempotent: safe to run multiple times.
Run with: python -m scripts.seed_demo
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

# Add project root to path
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config.settings import settings
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event
from src.models.session import Session
from src.models.tenant import Tenant, TenantConfig
from src.models.user import User, UserRole

# Same hashing context as auth middleware
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------------------------------------------------------------------------
# Fixed IDs for idempotency
# ---------------------------------------------------------------------------
TENANT_ID = UUID("d0000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("d0000000-0000-0000-0000-000000000010")
DEMO_API_KEY = "ns_dev_demo_key"
DEMO_TENANT_NAME = "Clinica Demo"

# ---------------------------------------------------------------------------
# User definitions
# ---------------------------------------------------------------------------
USERS = [
    {
        "email": "admin@demo.com",
        "password": "demo123",
        "role": UserRole.clinic_admin,
    },
    {
        "email": "super@demo.com",
        "password": "demo123",
        "role": UserRole.super_admin,
    },
    {
        "email": "investor@demo.com",
        "password": "demo123",
        "role": UserRole.investor,
    },
]

# ---------------------------------------------------------------------------
# Clinic config JSONB buckets
# ---------------------------------------------------------------------------
CLINIC_PERSONA = {
    "clinic_display_name": "Clínica Vida Nova",
    "agent_display_name": "Daniela",
    "use_personal_name": True,
}

CLINIC_IDENTITY = {
    "clinic_name": "Clínica Vida Nova",
    "agent_name": "Daniela",
    "main_domain": "medicina",
    "agent_presentation": "Sou a Daniela da Clínica Vida Nova",
    "clinic_specialties": ["dermatologia", "estética", "odontologia", "ortopedia"],
    "use_emojis": "yes",
}

CLINIC_OPERATIONS = {
    "full_address": "Rua Exemplo, 123",
    "working_hours": "8h-18h",
    "service_format": "both",
}

CLINIC_BUSINESS_RULES = {
    "accepts_insurance": "yes",
    "provide_prices": "yes",
    "differentials": "Atendimento humanizado",
}

CLINIC_SERVICES = [
    {
        "name": "Consulta Geral",
        "description": "Consulta clinica",
        "price": "R$ 200",
        "authority_notes": "",
    },
]

CLINIC_PROFESSIONALS = [
    {
        "name": "Dr. Joao Silva",
        "specialty": "Clinico Geral",
        "description": "10 anos de experiencia",
    },
]

# ---------------------------------------------------------------------------
# Sample events
# ---------------------------------------------------------------------------
SAMPLE_EVENTS = [
    {
        "event_type": "message.in",
        "event_data": {
            "content": "Ola, bom dia!",
            "channel": "whatsapp",
            "role": "user",
        },
        "event_metadata": {
            "intent": "saudacao",
            "funnel_stage": "lead_novo",
            "safety_concern": False,
            "handoff_required": False,
            "confidence": 0.95,
        },
    },
    {
        "event_type": "message.in",
        "event_data": {
            "content": "Gostaria de agendar uma consulta",
            "channel": "whatsapp",
            "role": "user",
        },
        "event_metadata": {
            "intent": "agendar",
            "funnel_stage": "em_qualificacao",
            "safety_concern": False,
            "handoff_required": False,
            "confidence": 0.92,
        },
    },
    {
        "event_type": "message.in",
        "event_data": {
            "content": "Qual o preco da consulta?",
            "channel": "whatsapp",
            "role": "user",
        },
        "event_metadata": {
            "intent": "pedido_preco",
            "funnel_stage": "qualificado",
            "safety_concern": False,
            "handoff_required": False,
            "confidence": 0.88,
        },
    },
    {
        "event_type": "message.in",
        "event_data": {
            "content": "Vou denunciar voces se nao me atenderem agora!",
            "channel": "whatsapp",
            "role": "user",
        },
        "event_metadata": {
            "intent": "conduta_inadequada_ameaca",
            "funnel_stage": "lead_novo",
            "safety_concern": True,
            "handoff_required": False,
            "confidence": 0.97,
        },
    },
    {
        "event_type": "message.in",
        "event_data": {
            "content": "Preciso falar com um humano para confirmar meu agendamento",
            "channel": "whatsapp",
            "role": "user",
        },
        "event_metadata": {
            "intent": "agendar",
            "funnel_stage": "agendamento_solicitado",
            "safety_concern": False,
            "handoff_required": True,
            "confidence": 0.90,
        },
    },
]


# ---------------------------------------------------------------------------
# Seed functions
# ---------------------------------------------------------------------------


async def seed_tenant(session: AsyncSession) -> Tenant:
    """Create demo tenant if it does not exist."""
    result = await session.execute(
        select(Tenant).where(Tenant.name == DEMO_TENANT_NAME)
    )
    existing = result.scalars().first()
    if existing:
        print(f"  [skip] Tenant already exists: {existing.name} ({existing.id})")
        return existing

    api_key_hash = pwd_context.hash(DEMO_API_KEY)

    config = TenantConfig(
        description="Demo tenant for admin panel testing",
        rate_limit=100,
        session_ttl_seconds=86400,
        max_sessions_per_user=10,
        features={
            "rag_enabled": True,
            "handoff_enabled": True,
            "safety_strict_mode": False,
        },
    )

    tenant = Tenant(
        id=TENANT_ID,
        name=DEMO_TENANT_NAME,
        slug="clinica-demo",
        api_key_hash=api_key_hash,
        is_active=True,
        config=config.to_dict(),
    )
    session.add(tenant)
    await session.flush()
    print(f"  [created] Tenant: {tenant.name} ({tenant.id})")
    return tenant


async def seed_users(session: AsyncSession, tenant_id: UUID) -> list[User]:
    """Create demo users if they do not exist."""
    created: list[User] = []
    for user_def in USERS:
        result = await session.execute(
            select(User).where(User.email == user_def["email"])
        )
        existing = result.scalars().first()
        if existing:
            print(f"  [skip] User already exists: {existing.email}")
            created.append(existing)
            continue

        user = User(
            id=uuid4(),
            tenant_id=tenant_id,
            email=user_def["email"],
            password_hash=pwd_context.hash(user_def["password"]),
            role=user_def["role"],
            is_active=True,
        )
        session.add(user)
        print(f"  [created] User: {user.email} ({user.role.value})")
        created.append(user)

    await session.flush()
    return created


async def seed_clinic_config(session: AsyncSession, tenant_id: UUID) -> ClinicConfigTable:
    """Create clinic config if it does not exist."""
    result = await session.execute(
        select(ClinicConfigTable).where(ClinicConfigTable.tenant_id == tenant_id)
    )
    existing = result.scalars().first()
    if existing:
        print(f"  [skip] ClinicConfig already exists for tenant {tenant_id}")
        return existing

    config = ClinicConfigTable(
        id=uuid4(),
        tenant_id=tenant_id,
        version=1,
        persona=CLINIC_PERSONA,
        identity=CLINIC_IDENTITY,
        operations=CLINIC_OPERATIONS,
        business_rules=CLINIC_BUSINESS_RULES,
        services=CLINIC_SERVICES,
        professionals=CLINIC_PROFESSIONALS,
        # Default empty for buckets not specified
        scheduling={},
        pricing={},
        post_care={},
        handoff={},
        lgpd={},
    )
    session.add(config)
    await session.flush()
    print(f"  [created] ClinicConfig for tenant {tenant_id}")
    return config


async def seed_session(session: AsyncSession, tenant_id: UUID) -> Session:
    """Create demo session if it does not exist."""
    result = await session.execute(
        select(Session).where(Session.id == SESSION_ID)
    )
    existing = result.scalars().first()
    if existing:
        print(f"  [skip] Session already exists: {existing.id}")
        return existing

    demo_session = Session(
        id=SESSION_ID,
        tenant_id=tenant_id,
        user_id="whatsapp:5511999990000",
        channel="whatsapp",
        cognitive_state={
            "funnel_stage": "lead_novo",
            "intent_history": [],
            "slots": {},
            "flags": {},
            "turn_count": 0,
            "safety_triggers": 0,
        },
        session_metadata={"source": "seed_demo"},
        is_active=True,
        message_count=0,
    )
    session.add(demo_session)
    await session.flush()
    print(f"  [created] Session: {demo_session.id}")
    return demo_session


async def seed_events(
    session: AsyncSession,
    tenant_id: UUID,
    session_id: UUID,
) -> list[Event]:
    """Create sample events if they do not exist for this session."""
    result = await session.execute(
        select(Event).where(Event.session_id == session_id)
    )
    existing = result.scalars().all()
    if existing:
        print(f"  [skip] Events already exist for session {session_id} ({len(existing)} events)")
        return list(existing)

    now = datetime.now(tz=timezone.utc)
    created: list[Event] = []
    for i, evt_def in enumerate(SAMPLE_EVENTS):
        event = Event(
            id=uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=evt_def["event_type"],
            event_data=evt_def["event_data"],
            event_metadata=evt_def["event_metadata"],
        )
        session.add(event)
        created.append(event)
        print(
            f"  [created] Event {i + 1}: "
            f"intent={evt_def['event_metadata'].get('intent')}, "
            f"funnel={evt_def['event_metadata'].get('funnel_stage')}"
        )

    await session.flush()
    return created


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    """Run all seed steps inside a single transaction."""
    print("=" * 60)
    print("NeuroStack Demo Seed")
    print("=" * 60)
    print(f"Database: {settings.database_url.split('@')[-1]}")
    print()

    engine = create_async_engine(settings.database_url)
    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        async with session.begin():
            print("[1/5] Tenant")
            tenant = await seed_tenant(session)

            print("[2/5] Users")
            await seed_users(session, tenant.id)

            print("[3/5] Clinic Config")
            await seed_clinic_config(session, tenant.id)

            print("[4/5] Session")
            demo_session = await seed_session(session, tenant.id)

            print("[5/5] Events")
            await seed_events(session, tenant.id, demo_session.id)

    await engine.dispose()

    print()
    print("=" * 60)
    print("Seed complete.")
    print(f"  API Key : {DEMO_API_KEY}")
    print(f"  Tenant  : {DEMO_TENANT_NAME} ({TENANT_ID})")
    print(f"  Users   : admin@demo.com / super@demo.com / investor@demo.com")
    print(f"  Password: demo123")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
