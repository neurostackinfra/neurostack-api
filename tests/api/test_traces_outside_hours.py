"""Integration tests for outside-hours metrics on trace statistics."""

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import jwt
import pytest
from httpx import AsyncClient

from src.config.settings import settings
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event
from src.models.tenant import Tenant
from src.repositories import get_db_session

_CLINIC_TZ = ZoneInfo("America/Sao_Paulo")
_OPERATIONS = {
    "timezone": "America/Sao_Paulo",
    "business_hours": {
        "mon": {"open": "09:00", "close": "18:00"},
        "tue": {"open": "09:00", "close": "18:00"},
        "wed": {"open": "09:00", "close": "18:00"},
        "thu": {"open": "09:00", "close": "18:00"},
        "fri": {"open": "09:00", "close": "18:00"},
    },
}


def _clinic_headers(tenant_id: UUID) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "outside-hours@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _recent_business_day() -> date:
    candidate = datetime.now(_CLINIC_TZ).date() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _clinic_time_utc(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=_CLINIC_TZ).astimezone(
        timezone.utc
    )


async def _seed_clinic(
    tenant_id: UUID,
    *,
    events: list[Event] | None = None,
) -> None:
    session = get_db_session()
    try:
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Trace Clinic {tenant_id.hex[:8]}",
                slug=f"trace-clinic-{tenant_id.hex}",
                api_key_hash=f"trace-hash-{tenant_id.hex}",
                is_active=True,
                bot_active=True,
                plan_type="pilot",
                config={},
            )
        )
        session.add(
            ClinicConfigTable(
                tenant_id=tenant_id,
                clinic_id=tenant_id,
                operations=_OPERATIONS,
            )
        )
        if events:
            session.add_all(events)
        await session.commit()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_configured_clinic_with_no_events_has_null_percentage(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    await _seed_clinic(tenant_id)

    response = await async_client.get(
        "/v1/traces/stats",
        headers=_clinic_headers(tenant_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_traces"] == 0
    assert data["outside_hours_count"] == 0
    assert data["outside_hours_pct"] is None
    assert data["conversational_trace_count"] == 0
    assert data["business_hours_configured"] is True
    assert data["avg_processing_time_ms"] == 0.0
    assert data["safety_flags_count"] == 0
    assert data["handoff_count"] == 0
    assert data["total_tokens_sum"] == 0
    assert data["model_breakdown"] == []
    assert data["intent_distribution"] == {}
    assert data["new_users_count"] == 0
    assert data["returning_users_count"] == 0
    assert data["new_user_interactions"] == 0
    assert data["returning_user_interactions"] == 0
    assert data["new_user_interaction_pct"] is None
    assert data["returning_user_interaction_pct"] is None
    assert data["identified_user_interactions"] == 0
    assert data["unidentified_user_interactions"] == 0
    assert "period_start" in data
    assert "period_end" in data


@pytest.mark.asyncio
async def test_stats_use_only_conversational_message_in_denominator(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    business_day = _recent_business_day()
    events = [
        Event(
            tenant_id=tenant_id,
            event_type="message.in",
            event_data={"content": "Dentro", "decision": {}},
            event_metadata={},
            created_at=_clinic_time_utc(business_day, 12),
        ),
        Event(
            tenant_id=tenant_id,
            event_type="message.in",
            event_data={"content": "Fora", "intent": "unknown"},
            event_metadata={},
            created_at=_clinic_time_utc(business_day, 20),
        ),
        Event(
            tenant_id=tenant_id,
            event_type="message.in",
            event_data={"content": "Sem marcador"},
            event_metadata={},
            created_at=_clinic_time_utc(business_day, 12),
        ),
    ]
    await _seed_clinic(tenant_id, events=events)

    response = await async_client.get(
        "/v1/traces/stats",
        headers=_clinic_headers(tenant_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_traces"] == 3
    assert data["outside_hours_count"] == 1
    assert data["conversational_trace_count"] == 2
    assert data["outside_hours_pct"] == 50.0
    assert data["business_hours_configured"] is True
