"""Contract tests for the audit window enforced on ``GET /v1/traces/export``."""

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import AsyncClient

from src.api.routes import traces as traces_route
from src.config.settings import settings
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event
from src.models.tenant import Tenant
from src.repositories import get_db_session

# 09:00 in Sao Paulo / 05:00 in Los Angeles: clinic "today" is 2026-10-07 in
# every zone used below, so the 6-month floor is 2026-04-07.
FROZEN_NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> "FrozenDatetime":
        value = cls(2026, 10, 7, 12, tzinfo=timezone.utc)
        return value if tz is None else value.astimezone(tz)


@pytest.fixture(autouse=True)
def _freeze_export_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(traces_route, "datetime", FrozenDatetime)


def _headers(
    role: str,
    tenant_id: UUID,
    *,
    builder_id: UUID | None = None,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "email": f"{role}@export-window.test",
        "iat": now,
        "exp": now + timedelta(hours=1),
        "jti": str(uuid4()),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _tenant(
    *,
    timezone_name: str | None = "America/Sao_Paulo",
    audit_extended_access: bool = False,
    builder_id: UUID | None = None,
) -> Tenant:
    tenant_id = uuid4()
    tenant = Tenant(
        id=tenant_id,
        name=f"Export Window Clinic {tenant_id.hex}",
        slug=f"export-window-{tenant_id.hex}",
        api_key_hash=f"export-window-hash-{tenant_id.hex}",
        is_active=True,
        bot_active=True,
        plan_type="pilot",
        config={},
        builder_id=builder_id,
        audit_extended_access=audit_extended_access,
    )
    if timezone_name is not None:
        tenant.clinic_config_row = ClinicConfigTable(
            tenant_id=tenant_id,
            operations={"timezone": timezone_name},
        )
    return tenant


def _event(tenant_id: UUID, created_at: datetime, label: str) -> Event:
    return Event(
        id=uuid4(),
        tenant_id=tenant_id,
        event_type="message.in",
        event_data={
            "content": label,
            "channel": "whatsapp",
            "decision": {"intent": "test"},
        },
        event_metadata={"safety_concern": False, "handoff_required": False},
        created_at=created_at,
    )


async def _seed(*entities: Any) -> None:
    session = get_db_session()
    try:
        session.add_all(list(entities))
        await session.commit()
    finally:
        await session.close()


async def _export(
    client: AsyncClient,
    clinic_id: UUID,
    /,
    *,
    headers: dict[str, str] | None = None,
    **params: str,
) -> Any:
    return await client.get(
        "/v1/traces/export",
        params=params,
        headers=headers or _headers("clinic_admin", clinic_id),
    )


def _labels(response: Any) -> set[str]:
    rows = list(csv.reader(io.StringIO(response.text)))
    return {row[2] for row in rows[1:]}


@pytest.mark.asyncio
async def test_sixty_inclusive_dates_pass_and_sixty_one_fail(
    async_client: AsyncClient,
) -> None:
    tenant = _tenant()
    await _seed(tenant)

    accepted = await _export(
        async_client,
        tenant.id,
        start_date="2026-08-09T00:00",
        end_date="2026-10-07T23:59",
    )
    rejected = await _export(
        async_client,
        tenant.id,
        start_date="2026-08-08T00:00",
        end_date="2026-10-07T23:59",
    )

    assert accepted.status_code == 200, accepted.text
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "export_window_60_days"}


@pytest.mark.asyncio
async def test_sixty_day_cap_applies_even_with_the_addon(
    async_client: AsyncClient,
) -> None:
    tenant = _tenant(audit_extended_access=True)
    await _seed(tenant)

    response = await _export(
        async_client,
        tenant.id,
        start_date="2025-01-01",
        end_date="2025-03-02",
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "export_window_60_days"}


@pytest.mark.asyncio
async def test_lookback_rejects_without_addon_and_accepts_with_it(
    async_client: AsyncClient,
) -> None:
    plain = _tenant()
    extended = _tenant(audit_extended_access=True)
    old_event = datetime(2026, 4, 8, 15, tzinfo=timezone.utc)
    await _seed(
        plain,
        extended,
        _event(plain.id, old_event, "plain-old"),
        _event(extended.id, old_event, "extended-old"),
    )
    params = {"start_date": "2026-04-06T00:00", "end_date": "2026-04-10T00:00"}

    plain_response = await _export(async_client, plain.id, **params)
    extended_response = await _export(async_client, extended.id, **params)
    at_floor = await _export(
        async_client,
        plain.id,
        start_date="2026-04-07T00:00",
        end_date="2026-04-10T00:00",
    )

    assert plain_response.status_code == 400
    assert plain_response.json() == {"detail": "export_lookback_6_months"}
    assert extended_response.status_code == 200, extended_response.text
    assert _labels(extended_response) == {"extended-old"}
    assert at_floor.status_code == 200, at_floor.text
    assert _labels(at_floor) == {"plain-old"}


@pytest.mark.asyncio
async def test_no_dates_export_since_clinic_today_minus_30_until_now(
    async_client: AsyncClient,
) -> None:
    tenant = _tenant()
    await _seed(
        tenant,
        # 2026-09-07 00:00 in Sao Paulo is 03:00 UTC.
        _event(tenant.id, datetime(2026, 9, 7, 2, 59, 59, tzinfo=timezone.utc), "x1"),
        _event(tenant.id, datetime(2026, 9, 7, 3, tzinfo=timezone.utc), "start"),
        _event(tenant.id, datetime(2026, 10, 7, 11, tzinfo=timezone.utc), "recent"),
        _event(tenant.id, datetime(2026, 10, 7, 13, tzinfo=timezone.utc), "future"),
    )

    response = await _export(async_client, tenant.id)

    assert response.status_code == 200, response.text
    assert _labels(response) == {"start", "recent"}


@pytest.mark.asyncio
async def test_partial_ranges_keep_the_default_for_the_missing_side(
    async_client: AsyncClient,
) -> None:
    tenant = _tenant()
    await _seed(
        tenant,
        _event(tenant.id, datetime(2026, 9, 10, 15, tzinfo=timezone.utc), "sep-10"),
        _event(tenant.id, datetime(2026, 10, 2, 15, tzinfo=timezone.utc), "oct-02"),
        _event(tenant.id, datetime(2026, 10, 7, 11, tzinfo=timezone.utc), "recent"),
        _event(tenant.id, datetime(2026, 10, 7, 13, tzinfo=timezone.utc), "future"),
    )

    start_only = await _export(async_client, tenant.id, start_date="2026-10-01T00:00")
    end_only = await _export(async_client, tenant.id, end_date="2026-09-20T00:00")
    end_before_default_start = await _export(
        async_client, tenant.id, end_date="2026-09-01T00:00"
    )

    assert start_only.status_code == 200, start_only.text
    assert _labels(start_only) == {"oct-02", "recent"}
    assert end_only.status_code == 200, end_only.text
    assert _labels(end_only) == {"sep-10"}
    assert end_before_default_start.status_code == 400
    assert end_before_default_start.json() == {"detail": "invalid_date_range"}


@pytest.mark.asyncio
async def test_naive_values_count_by_their_own_date(
    async_client: AsyncClient,
) -> None:
    utc_clinic = _tenant(timezone_name="UTC")
    pacific_clinic = _tenant(timezone_name="America/Los_Angeles")
    await _seed(utc_clinic, pacific_clinic)

    # 23:30 naive is 02:30 UTC the next day; converting it would make 61 dates.
    late_evening_end = await _export(
        async_client,
        utc_clinic.id,
        start_date="2026-08-09T00:00",
        end_date="2026-10-07T23:30",
    )
    # 00:30 naive would be the previous day in Los Angeles (before the floor).
    early_floor_start = await _export(
        async_client,
        pacific_clinic.id,
        start_date="2026-04-07T00:30",
        end_date="2026-04-08T00:00",
    )

    assert late_evening_end.status_code == 200, late_evening_end.text
    assert early_floor_start.status_code == 200, early_floor_start.text


@pytest.mark.asyncio
async def test_aware_values_are_converted_to_the_clinic_zone(
    async_client: AsyncClient,
) -> None:
    tenant = _tenant()
    await _seed(tenant)

    # 02:00 UTC is still 2026-08-08 in Sao Paulo: 61 clinic dates.
    shifted_start = await _export(
        async_client,
        tenant.id,
        start_date="2026-08-09T02:00:00Z",
        end_date="2026-10-07T10:00:00Z",
    )
    local_start = await _export(
        async_client,
        tenant.id,
        start_date="2026-08-09T03:00:00Z",
        end_date="2026-10-07T10:00:00Z",
    )
    # A midnight end is stretched to 23:59:59 before the query, so the civil
    # date is taken after that stretch (2026-10-07 in Sao Paulo, not 10-06).
    stretched_end = await _export(
        async_client,
        tenant.id,
        start_date="2026-08-08T03:00:00Z",
        end_date="2026-10-07T00:00:00Z",
    )
    floor_in_clinic_zone = await _export(
        async_client,
        tenant.id,
        start_date="2026-04-07T02:59:59Z",
        end_date="2026-04-08T12:00:00Z",
    )

    assert shifted_start.status_code == 400
    assert shifted_start.json() == {"detail": "export_window_60_days"}
    assert local_start.status_code == 200, local_start.text
    assert stretched_end.status_code == 400
    assert stretched_end.json() == {"detail": "export_window_60_days"}
    assert floor_in_clinic_zone.status_code == 400
    assert floor_in_clinic_zone.json() == {"detail": "export_lookback_6_months"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "9999-12-31T23:00", "end_date": "9999-12-31T23:00"},
        {"start_date": "9999-12-31", "end_date": "9999-12-31"},
        {"start_date": "0001-01-01T00:00:00Z"},
        {"end_date": "0001-01-01T00:00:00Z"},
    ],
)
async def test_unrepresentable_bounds_return_invalid_date_range(
    async_client: AsyncClient,
    params: dict[str, str],
) -> None:
    tenant = _tenant(audit_extended_access=True)
    await _seed(tenant)

    response = await _export(async_client, tenant.id, **params)

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_date_range"}


@pytest.mark.asyncio
async def test_builder_override_uses_the_target_clinic_flag(
    async_client: AsyncClient,
) -> None:
    builder_id = uuid4()
    builder = Builder(id=builder_id, name="Export Window Builder")
    # The builder's own tenant holds the add-on; only the target's flag counts.
    builder_home = _tenant(audit_extended_access=True, builder_id=builder_id)
    plain = _tenant(builder_id=builder_id)
    extended = _tenant(audit_extended_access=True, builder_id=builder_id)
    await _seed(builder, builder_home, plain, extended)
    headers = _headers("builder", builder_home.id, builder_id=builder_id)
    params = {"start_date": "2026-01-01T00:00", "end_date": "2026-01-31T00:00"}

    plain_response = await _export(
        async_client, plain.id, headers=headers, tenant_id=str(plain.id), **params
    )
    extended_response = await _export(
        async_client,
        extended.id,
        headers=headers,
        tenant_id=str(extended.id),
        **params,
    )

    assert plain_response.status_code == 400
    assert plain_response.json() == {"detail": "export_lookback_6_months"}
    assert extended_response.status_code == 200, extended_response.text


@pytest.mark.asyncio
async def test_super_admin_override_uses_the_target_clinic_flag(
    async_client: AsyncClient,
) -> None:
    admin_home = _tenant(audit_extended_access=True)
    plain = _tenant()
    extended = _tenant(audit_extended_access=True)
    await _seed(admin_home, plain, extended)
    headers = _headers("super_admin", admin_home.id)
    params = {"start_date": "2026-03-01T00:00", "end_date": "2026-03-10T00:00"}

    plain_response = await _export(
        async_client, plain.id, headers=headers, tenant_id=str(plain.id), **params
    )
    extended_response = await _export(
        async_client,
        extended.id,
        headers=headers,
        tenant_id=str(extended.id),
        **params,
    )

    assert plain_response.status_code == 400
    assert plain_response.json() == {"detail": "export_lookback_6_months"}
    assert extended_response.status_code == 200, extended_response.text


@pytest.mark.asyncio
async def test_trace_list_and_stats_ignore_export_lookback(
    async_client: AsyncClient,
) -> None:
    tenant = _tenant()
    old_event = datetime(2026, 3, 10, 15, tzinfo=timezone.utc)
    await _seed(tenant, _event(tenant.id, old_event, "seven-months-old"))
    headers = _headers("clinic_admin", tenant.id)
    params = {"start_date": "2026-03-07T00:00", "end_date": "2026-03-20T00:00"}

    listed = await async_client.get("/v1/traces", params=params, headers=headers)
    stats = await async_client.get("/v1/traces/stats", params=params, headers=headers)
    exported = await _export(async_client, tenant.id, **params)

    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert stats.status_code == 200, stats.text
    assert exported.status_code == 400
    assert exported.json() == {"detail": "export_lookback_6_months"}


def test_export_openapi_documents_the_window_codes() -> None:
    from src.api.main import app

    operation = app.openapi()["paths"]["/v1/traces/export"]["get"]
    examples = operation["responses"]["400"]["content"]["application/json"]["examples"]

    assert set(examples) == {
        "invalid_date_range",
        "export_window_60_days",
        "export_lookback_6_months",
    }
    assert "30" in operation["description"]
