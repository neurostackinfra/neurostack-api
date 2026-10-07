"""Integration tests for builder-scoped outside-hours clinic metrics."""

from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import jwt
import pytest
from httpx import AsyncClient

from src.config.settings import settings
from src.models.builder import Builder
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


def _super_admin_headers() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "super_admin",
            "email": "admin-outside-hours@test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def _create_builder_authority(
    client: AsyncClient,
) -> tuple[UUID, dict[str, str]]:
    email = f"builder-{uuid4().hex}@test.com"
    password = "builder-test-password"
    created = await client.post(
        "/v1/admin/builders",
        headers=_super_admin_headers(),
        json={
            "name": f"Builder {uuid4().hex[:8]}",
            "admin_email": email,
            "admin_password": password,
        },
    )
    assert created.status_code == 201, created.text

    logged_in = await client.post(
        "/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert logged_in.status_code == 200, logged_in.text
    return UUID(created.json()["id"]), {
        "Authorization": f"Bearer {logged_in.json()['token']}"
    }


def _recent_business_day() -> date:
    candidate = datetime.now(_CLINIC_TZ).date() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _clinic_time_utc(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour=hour), tzinfo=_CLINIC_TZ).astimezone(
        timezone.utc
    )


def _tenant(tenant_id: UUID, builder_id: UUID, name: str) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=name,
        slug=f"builder-clinic-{tenant_id.hex}",
        api_key_hash=f"builder-hash-{tenant_id.hex}",
        is_active=True,
        bot_active=True,
        builder_id=builder_id,
        plan_type="pilot",
        config={},
    )


@pytest.mark.asyncio
async def test_builder_metrics_are_conversational_and_tenant_scoped(
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SQLite drops timezone information from DateTime columns; isolate the
    # existing health calculation so this test exercises the new metric path.
    monkeypatch.setattr(
        "src.api.routes.builder._compute_health_status",
        lambda _last_trace, _is_active: "healthy",
    )
    owner_builder_id, builder_headers = await _create_builder_authority(async_client)
    other_builder_id = uuid4()
    owned_tenant_id = uuid4()
    other_tenant_id = uuid4()
    business_day = _recent_business_day()

    session = get_db_session()
    try:
        session.add_all(
            [
                Builder(
                    id=other_builder_id,
                    name="Other Builder",
                    status="active",
                    is_active=True,
                ),
                _tenant(owned_tenant_id, owner_builder_id, "Owned Clinic"),
                _tenant(other_tenant_id, other_builder_id, "Other Clinic"),
                ClinicConfigTable(
                    tenant_id=owned_tenant_id,
                    clinic_id=owned_tenant_id,
                    operations=_OPERATIONS,
                ),
                ClinicConfigTable(
                    tenant_id=other_tenant_id,
                    clinic_id=other_tenant_id,
                    operations=_OPERATIONS,
                ),
            ]
        )
        session.add_all(
            [
                Event(
                    tenant_id=owned_tenant_id,
                    event_type="message.in",
                    event_data={"content": "Dentro", "decision": {}},
                    event_metadata={},
                    created_at=_clinic_time_utc(business_day, 12),
                ),
                Event(
                    tenant_id=owned_tenant_id,
                    event_type="message.in",
                    event_data={"content": "Fora", "intent": "unknown"},
                    event_metadata={},
                    created_at=_clinic_time_utc(business_day, 20),
                ),
                Event(
                    tenant_id=owned_tenant_id,
                    event_type="message.in",
                    event_data={"content": "Sem marcador"},
                    event_metadata={},
                    created_at=_clinic_time_utc(business_day, 12),
                ),
                Event(
                    tenant_id=other_tenant_id,
                    event_type="message.in",
                    event_data={"intent": "leak-if-read"},
                    event_metadata={},
                    created_at=_clinic_time_utc(business_day, 20),
                ),
            ]
        )
        await session.commit()
    finally:
        await session.close()

    response = await async_client.get(
        "/v1/builder/clinics",
        headers=builder_headers,
    )

    assert response.status_code == 200
    clinics = response.json()
    assert [clinic["id"] for clinic in clinics] == [str(owned_tenant_id)]
    clinic = clinics[0]
    assert clinic["trace_count_7d"] == 3
    assert clinic["outside_hours_count_7d"] == 1
    assert clinic["conversational_trace_count_7d"] == 2
    assert clinic["outside_hours_pct_7d"] == 50.0
    assert clinic["business_hours_configured"] is True
