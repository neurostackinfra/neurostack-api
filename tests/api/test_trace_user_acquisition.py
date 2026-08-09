"""Integration tests for commercial user-acquisition trace metrics."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import AsyncClient

from src.config.settings import settings
from src.models.event import Event
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories import get_db_session

_PERIOD_START = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)


def _clinic_headers(tenant_id: UUID) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "commercial-metrics@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _tenant(tenant_id: UUID) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=f"Acquisition Clinic {tenant_id.hex[:8]}",
        slug=f"acquisition-clinic-{tenant_id.hex}",
        api_key_hash=f"acquisition-hash-{tenant_id.hex}",
        is_active=True,
        bot_active=True,
        plan_type="pilot",
        config={},
    )


def _message(
    tenant_id: UUID,
    created_at: datetime,
    *,
    session_id: UUID | None = None,
    user_id: str | None = None,
) -> Event:
    event_data = {"content": "Olá"}
    if user_id is not None:
        event_data["user_id"] = user_id
    return Event(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="message.in",
        event_data=event_data,
        event_metadata={},
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_user_acquisition_classifies_first_ever_interaction(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    returning_session_id = uuid4()
    new_session_id = uuid4()

    session = get_db_session()
    try:
        session.add(_tenant(tenant_id))
        session.add_all(
            [
                Session(
                    id=returning_session_id,
                    tenant_id=tenant_id,
                    user_id="+5511999990001",
                    channel="whatsapp",
                ),
                Session(
                    id=new_session_id,
                    tenant_id=tenant_id,
                    user_id="+5511999990002",
                    channel="whatsapp",
                ),
            ]
        )
        session.add_all(
            [
                # This historical interaction makes the first phone recurring.
                _message(
                    tenant_id,
                    _PERIOD_START - timedelta(days=5),
                    session_id=returning_session_id,
                ),
                _message(
                    tenant_id,
                    _PERIOD_START,
                    session_id=returning_session_id,
                ),
                _message(
                    tenant_id,
                    _PERIOD_START + timedelta(days=1),
                    session_id=returning_session_id,
                ),
                # Event data has no user_id, so both interactions use Session.user_id.
                _message(
                    tenant_id,
                    _PERIOD_START,
                    session_id=new_session_id,
                ),
                _message(
                    tenant_id,
                    _PERIOD_START + timedelta(days=2),
                    session_id=new_session_id,
                ),
                # A username can identify an interaction even without a session.
                _message(
                    tenant_id,
                    _PERIOD_START + timedelta(days=1),
                    user_id="instagram:new-prospect",
                ),
                # Anonymous interactions remain visible but do not skew percentages.
                _message(
                    tenant_id,
                    _PERIOD_START + timedelta(days=1),
                ),
            ]
        )
        await session.commit()
    finally:
        await session.close()

    response = await async_client.get(
        "/v1/traces/stats?start_date=2026-01-10&end_date=2026-01-12",
        headers=_clinic_headers(tenant_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_traces"] == 6
    assert data["new_users_count"] == 2
    assert data["returning_users_count"] == 1
    assert data["new_user_interactions"] == 3
    assert data["returning_user_interactions"] == 2
    assert data["identified_user_interactions"] == 5
    assert data["unidentified_user_interactions"] == 1
    assert data["new_user_interaction_pct"] == 60.0
    assert data["returning_user_interaction_pct"] == 40.0


@pytest.mark.asyncio
async def test_first_interaction_is_scoped_to_the_current_clinic(
    async_client: AsyncClient,
) -> None:
    current_tenant_id = uuid4()
    other_tenant_id = uuid4()
    shared_username = "instagram:shared-user"

    session = get_db_session()
    try:
        session.add_all([_tenant(current_tenant_id), _tenant(other_tenant_id)])
        session.add_all(
            [
                _message(
                    other_tenant_id,
                    _PERIOD_START - timedelta(days=30),
                    user_id=shared_username,
                ),
                _message(
                    current_tenant_id,
                    _PERIOD_START,
                    user_id=shared_username,
                ),
            ]
        )
        await session.commit()
    finally:
        await session.close()

    response = await async_client.get(
        "/v1/traces/stats?start_date=2026-01-10&end_date=2026-01-12",
        headers=_clinic_headers(current_tenant_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["new_users_count"] == 1
    assert data["returning_users_count"] == 0
    assert data["new_user_interactions"] == 1
    assert data["returning_user_interactions"] == 0
    assert data["new_user_interaction_pct"] == 100.0
    assert data["returning_user_interaction_pct"] == 0.0


@pytest.mark.asyncio
async def test_percentages_are_null_when_no_interaction_has_an_identity(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    session = get_db_session()
    try:
        session.add(_tenant(tenant_id))
        session.add(_message(tenant_id, _PERIOD_START))
        await session.commit()
    finally:
        await session.close()

    response = await async_client.get(
        "/v1/traces/stats?start_date=2026-01-10&end_date=2026-01-12",
        headers=_clinic_headers(tenant_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["new_users_count"] == 0
    assert data["returning_users_count"] == 0
    assert data["identified_user_interactions"] == 0
    assert data["unidentified_user_interactions"] == 1
    assert data["new_user_interaction_pct"] is None
    assert data["returning_user_interaction_pct"] is None
