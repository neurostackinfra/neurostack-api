"""Contract tests for exact-day trace history queries."""

import csv
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import AsyncClient

from src.api.main import app
from src.config.settings import settings
from src.models.event import Event
from src.models.tenant import Tenant
from src.repositories import get_db_session


def _clinic_headers(tenant_id: UUID) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "daily-history@test.com",
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
        name=f"Daily Trace Clinic {tenant_id.hex}",
        slug=f"daily-trace-{tenant_id.hex}",
        api_key_hash=f"daily-trace-hash-{tenant_id.hex}",
        is_active=True,
        bot_active=True,
        plan_type="pilot",
        config={},
    )


def _event(
    tenant_id: UUID,
    created_at: datetime,
    label: str,
    *,
    event_id: UUID | None = None,
    channel: str = "whatsapp",
    safety_flag: bool = False,
    handoff_required: bool = False,
    decision: dict[str, object] | None = None,
) -> Event:
    return Event(
        id=event_id or uuid4(),
        tenant_id=tenant_id,
        event_type="message.in",
        event_data={
            "content": label,
            "channel": channel,
            "decision": {"intent": "test"} if decision is None else decision,
        },
        event_metadata={
            "safety_concern": safety_flag,
            "handoff_required": handoff_required,
        },
        created_at=created_at,
    )


async def _seed(*tenants: Tenant, events: list[Event]) -> None:
    session = get_db_session()
    try:
        session.add_all([*tenants, *events])
        await session.commit()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_daily_window_is_half_open_and_filters_before_count(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    other_tenant_id = uuid4()
    start = datetime(2026, 9, 28, 3, tzinfo=timezone.utc)
    end = datetime(2026, 9, 29, 3, tzinfo=timezone.utc)
    await _seed(
        _tenant(tenant_id),
        _tenant(other_tenant_id),
        events=[
            _event(tenant_id, start - timedelta(microseconds=1), "before"),
            _event(
                tenant_id,
                start,
                "at-start",
                safety_flag=True,
                handoff_required=True,
            ),
            _event(
                tenant_id,
                end - timedelta(microseconds=1),
                "before-end",
                safety_flag=True,
                handoff_required=True,
            ),
            _event(tenant_id, end, "at-end"),
            _event(
                tenant_id,
                start + timedelta(hours=1),
                "wrong-channel",
                channel="web",
                safety_flag=True,
                handoff_required=True,
            ),
            _event(
                tenant_id,
                start + timedelta(hours=1),
                "wrong-safety",
                handoff_required=True,
            ),
            _event(
                tenant_id,
                start + timedelta(hours=1),
                "wrong-handoff",
                safety_flag=True,
            ),
            _event(
                other_tenant_id,
                start + timedelta(hours=2),
                "other-tenant",
                safety_flag=True,
                handoff_required=True,
            ),
        ],
    )

    response = await async_client.get(
        "/v1/traces",
        params={
            "day": "2026-09-28",
            "timezone": "America/Sao_Paulo",
            "channel": "whatsapp",
            "safety_flag": "true",
            "handoff_required": "true",
            "limit": 1,
            "offset": 1,
        },
        headers=_clinic_headers(tenant_id),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert [item["user_message"] for item in body["items"]] == ["at-start"]


@pytest.mark.asyncio
async def test_daily_pagination_is_stable_for_equal_timestamps(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    tied_at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    events = [
        _event(
            tenant_id,
            tied_at,
            f"trace-{number}",
            event_id=UUID(int=(10 << 124) + number),
        )
        for number in range(1, 56)
    ]
    await _seed(_tenant(tenant_id), events=events)
    params: dict[str, str | int] = {
        "day": "2026-09-28",
        "timezone": "America/Sao_Paulo",
        "limit": 50,
        "offset": 50,
    }

    first = await async_client.get(
        "/v1/traces", params=params, headers=_clinic_headers(tenant_id)
    )
    repeated = await async_client.get(
        "/v1/traces", params=params, headers=_clinic_headers(tenant_id)
    )

    assert first.status_code == 200
    assert first.json()["total"] == 55
    assert len(first.json()["items"]) == 5
    expected_ids = [str(UUID(int=(10 << 124) + number)) for number in range(5, 0, -1)]
    assert [item["id"] for item in first.json()["items"]] == expected_ids
    assert repeated.json()["items"] == first.json()["items"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"day": "2026-09-28"}, "timezone_required_with_day"),
        ({"timezone": "America/Sao_Paulo"}, "day_required_with_timezone"),
        (
            {"day": "2026-09-28", "timezone": "Not/A_Real_Zone"},
            "invalid_timezone",
        ),
        (
            {"day": "2026-09-28", "timezone": "localtime"},
            "invalid_timezone",
        ),
        (
            {"day": "2026-09-28", "timezone": "posixrules"},
            "invalid_timezone",
        ),
        (
            {
                "day": "2026-09-28",
                "timezone": "America/Sao_Paulo",
                "start_date": "2026-09-01",
            },
            "day_conflicts_with_date_range",
        ),
        (
            {
                "day": "2026-09-28",
                "timezone": "America/Sao_Paulo",
                "start_date": "2026-09-01",
                "end_date": "2026-09-30",
            },
            "day_conflicts_with_date_range",
        ),
        (
            {
                "day": "2026-09-28",
                "timezone": "America/Sao_Paulo",
                "end_date": "2026-09-30",
            },
            "day_conflicts_with_date_range",
        ),
    ],
)
async def test_daily_query_rejects_invalid_combinations(
    async_client: AsyncClient,
    params: dict[str, str],
    detail: str,
) -> None:
    response = await async_client.get(
        "/v1/traces", params=params, headers=_clinic_headers(uuid4())
    )

    assert response.status_code == 400
    assert response.json() == {"detail": detail}


@pytest.mark.asyncio
async def test_invalid_day_uses_fastapi_query_validation(
    async_client: AsyncClient,
) -> None:
    response = await async_client.get(
        "/v1/traces",
        params={"day": "not-a-date", "timezone": "America/Sao_Paulo"},
        headers=_clinic_headers(uuid4()),
    )

    assert response.status_code == 422
    assert any(error["loc"] == ["query", "day"] for error in response.json()["detail"])


@pytest.mark.asyncio
@pytest.mark.parametrize("day", ["0001-01-01", "9999-12-31"])
async def test_extreme_day_uses_fastapi_query_validation(
    async_client: AsyncClient,
    day: str,
) -> None:
    response = await async_client.get(
        "/v1/traces",
        params={"day": day, "timezone": "Asia/Tokyo"},
        headers=_clinic_headers(uuid4()),
    )

    assert response.status_code == 422
    assert any(error["loc"] == ["query", "day"] for error in response.json()["detail"])


def test_daily_query_is_documented_in_openapi() -> None:
    operation = app.openapi()["paths"]["/v1/traces"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    day_schema = parameters["day"]["schema"]
    assert day_schema["anyOf"][0] == {
        "type": "string",
        "format": "date",
    }
    assert day_schema["minimum"] == "0001-01-02"
    assert day_schema["maximum"] == "9999-12-30"
    assert parameters["timezone"]["schema"]["anyOf"][0] == {"type": "string"}
    assert "IANA" in parameters["timezone"]["description"]
    assert "Requires timezone" in parameters["day"]["description"]
    assert "cannot be combined" in parameters["day"]["description"]
    responses = operation["responses"]
    examples_400 = responses["400"]["content"]["application/json"]["examples"]
    assert {example["value"]["detail"] for example in examples_400.values()} == {
        "timezone_required_with_day",
        "day_required_with_timezone",
        "invalid_timezone",
        "day_conflicts_with_date_range",
    }
    assert responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HTTPValidationError"
    }

    export_parameters = {
        parameter["name"]
        for parameter in app.openapi()["paths"]["/v1/traces/export"]["get"][
            "parameters"
        ]
    }
    assert "day" not in export_parameters
    assert "timezone" not in export_parameters


@pytest.mark.asyncio
async def test_legacy_date_matrix_and_envelope_are_unchanged(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    await _seed(
        _tenant(tenant_id),
        events=[
            _event(
                tenant_id,
                datetime(2026, 9, 27, 2, 59, 59, 999999, tzinfo=timezone.utc),
                "before-start",
            ),
            _event(
                tenant_id,
                datetime(2026, 9, 27, 3, tzinfo=timezone.utc),
                "at-start",
            ),
            _event(
                tenant_id,
                datetime(2026, 9, 27, 15, tzinfo=timezone.utc),
                "middle",
            ),
            _event(
                tenant_id,
                datetime(2026, 9, 28, 2, 59, 59, tzinfo=timezone.utc),
                "at-expanded-end",
            ),
            _event(
                tenant_id,
                datetime(2026, 9, 28, 2, 59, 59, 1, tzinfo=timezone.utc),
                "after-expanded-end",
            ),
            _event(
                tenant_id,
                datetime(2026, 9, 28, 3, tzinfo=timezone.utc),
                "next-day",
            ),
        ],
    )
    headers = _clinic_headers(tenant_id)

    unbounded = await async_client.get("/v1/traces", headers=headers)
    only_start = await async_client.get(
        "/v1/traces", params={"start_date": "2026-09-27"}, headers=headers
    )
    only_end = await async_client.get(
        "/v1/traces", params={"end_date": "2026-09-27"}, headers=headers
    )
    both = await async_client.get(
        "/v1/traces",
        params={"start_date": "2026-09-27", "end_date": "2026-09-27"},
        headers=headers,
    )

    assert unbounded.status_code == 200
    assert set(unbounded.json()) == {"items", "total", "limit", "offset"}
    assert unbounded.json()["total"] == 6
    assert unbounded.json()["limit"] == 20
    assert unbounded.json()["offset"] == 0
    assert only_start.json()["total"] == 5
    assert only_end.json()["total"] == 4
    assert both.json()["total"] == 3


@pytest.mark.asyncio
async def test_export_still_supports_a_multi_day_range(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    await _seed(
        _tenant(tenant_id),
        events=[
            _event(
                tenant_id,
                datetime(2026, 9, 27, 15, tzinfo=timezone.utc),
                "first-day",
            ),
            _event(
                tenant_id,
                datetime(2026, 9, 28, 15, tzinfo=timezone.utc),
                "second-day",
            ),
        ],
    )

    response = await async_client.get(
        "/v1/traces/export",
        params={"start_date": "2026-09-27", "end_date": "2026-09-28"},
        headers=_clinic_headers(tenant_id),
    )

    assert response.status_code == 200
    assert "first-day" in response.text
    assert "second-day" in response.text


@pytest.mark.asyncio
async def test_export_omits_intent_category_and_action_columns(
    async_client: AsyncClient,
) -> None:
    tenant_id = uuid4()
    await _seed(
        _tenant(tenant_id),
        events=[
            _event(
                tenant_id,
                datetime(2026, 9, 28, 15, tzinfo=timezone.utc),
                "exported-trace",
                decision={
                    "intent": "test-intent",
                    "intent_category": "must-not-be-exported",
                    "funnel_stage": "qualification",
                    "action": "must-not-be-exported",
                    "confidence": 0.91,
                },
            )
        ],
    )

    response = await async_client.get(
        "/v1/traces/export",
        headers=_clinic_headers(tenant_id),
    )

    assert response.status_code == 200
    rows = list(csv.reader(response.text.splitlines()))
    assert rows[0] == [
        "id",
        "timestamp",
        "user_message",
        "intent",
        "funnel_stage",
        "confidence",
        "safety_concern",
        "handoff_required",
        "processing_time_ms",
        "mode",
        "context_summary",
    ]
    assert rows[1][2:6] == [
        "exported-trace",
        "test-intent",
        "qualification",
        "0.91",
    ]
    assert "must-not-be-exported" not in rows[1]
