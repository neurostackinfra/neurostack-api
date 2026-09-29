"""Contract tests for the handoff entry-cohort CSV export."""

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import pwd_context
from src.config.settings import settings
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.database import get_db_session


def _jwt_headers(
    role: str,
    tenant_id: UUID,
    *,
    builder_id: UUID | None = None,
) -> dict[str, str]:
    payload = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _tenant(
    *,
    builder_id: UUID | None = None,
    timezone_name: str | None = "UTC",
) -> Tenant:
    tenant_id = uuid4()
    tenant = Tenant(
        id=tenant_id,
        name=f"Clinic {tenant_id}",
        slug=f"clinic-{tenant_id}",
        api_key_hash=f"hash-{tenant_id}",
        builder_id=builder_id,
        config={},
    )
    if timezone_name is not None:
        tenant.clinic_config_row = ClinicConfigTable(
            tenant_id=tenant_id,
            operations={"timezone": timezone_name},
        )
    return tenant


def _session(tenant_id: UUID) -> Session:
    return Session(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=f"export-{uuid4()}",
        channel="whatsapp",
        cognitive_state={},
        session_metadata={},
        last_activity_at=datetime.now(timezone.utc),
        is_active=True,
        is_paused=True,
    )


def _handoff(
    tenant_id: UUID,
    session_id: UUID,
    *,
    created_at: datetime,
    status: str = "pending",
    checked_out_at: datetime | None = None,
    phone: str | None = None,
) -> HandoffRecord:
    return HandoffRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        reason="user_request",
        summary="export contract",
        context={},
        status=status,
        urgency="normal",
        numero_whatsapp=phone or str(uuid4()),
        created_at=created_at,
        data_hora_saida=checked_out_at,
    )


def _seed(client: TestClient, *entities: Any) -> None:
    async def _run() -> None:
        db = get_db_session()
        try:
            db.add_all(entities)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _seed_in_batches(
    client: TestClient, entities: list[HandoffRecord], batch_size: int = 1000
) -> None:
    async def _run() -> None:
        db = get_db_session()
        try:
            for start in range(0, len(entities), batch_size):
                db.add_all(entities[start : start + batch_size])
                await db.commit()
                db.expunge_all()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _export(
    client: TestClient,
    tenant_id: UUID,
    *,
    headers: dict[str, str] | None = None,
    **params: str,
) -> Any:
    return client.get(
        "/v1/handoffs/queue/export",
        params={"tenant_id": str(tenant_id), **params},
        headers=headers or _jwt_headers("clinic_admin", tenant_id),
    )


def _csv_rows(response: Any) -> tuple[list[str], list[dict[str, str]]]:
    text = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader)


def test_export_empty_preserves_bom_headers_defaults_and_window_limit(
    client: TestClient,
) -> None:
    tenant = _tenant()
    _seed(client, tenant)

    response = _export(client, tenant.id)

    today = datetime.now(timezone.utc).date()
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="handoffs_{today - timedelta(days=30)}_{today}.csv"'
    )
    header, rows = _csv_rows(response)
    assert header == [
        "numero_whatsapp",
        "session_id",
        "created_at",
        "data_hora_saida",
        "tempo_espera_segundos",
        "status",
        "exported_at",
    ]
    assert rows == []

    accepted = _export(
        client,
        tenant.id,
        from_date="2026-01-01",
        to_date="2026-04-01",
    )
    rejected = _export(
        client,
        tenant.id,
        from_date="2026-01-01",
        to_date="2026-04-02",
    )
    assert accepted.status_code == 200
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "max_window_90_days"}


def test_export_uses_entry_cohort_states_waits_utc_and_descending_order(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.routes import handoff as route

    exported_at = datetime(2026, 1, 12, 12, tzinfo=timezone.utc)
    now_calls = 0
    query_completed = False
    selected_columns: list[str] = []
    original_execute = AsyncSession.execute

    async def tracked_execute(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        nonlocal query_completed, selected_columns
        selected_columns = [column.key for column in args[0].selected_columns]
        result = await original_execute(self, *args, **kwargs)
        query_completed = True
        return result

    class ExportDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            nonlocal now_calls
            assert (
                query_completed
            ), "export clock must be captured after query materialization"
            now_calls += 1
            return exported_at

    monkeypatch.setattr(AsyncSession, "execute", tracked_execute)
    monkeypatch.setattr(route, "datetime", ExportDatetime)

    tenant = _tenant()
    other_tenant = _tenant()
    session = _session(tenant.id)
    other_session = _session(other_tenant.id)
    pending = _handoff(
        tenant.id,
        session.id,
        created_at=datetime(2026, 1, 10, 8, 0, 0, 400000, tzinfo=timezone.utc),
        checked_out_at=datetime(2026, 1, 10, 8, 1, tzinfo=timezone.utc),
        phone="pending",
    )
    accepted = _handoff(
        tenant.id,
        session.id,
        created_at=datetime(2026, 1, 10, 10, tzinfo=timezone.utc),
        status="accepted",
        checked_out_at=datetime(2026, 1, 10, 10, 1, tzinfo=timezone.utc),
        phone="accepted",
    )
    completed = _handoff(
        tenant.id,
        session.id,
        created_at=datetime(2026, 1, 10, 9, tzinfo=timezone.utc),
        status="completed",
        checked_out_at=datetime(2026, 1, 11, 9, 0, 30, 260000, tzinfo=timezone.utc),
        phone="completed-after-window",
    )
    confirmed_in_progress = _handoff(
        tenant.id,
        session.id,
        created_at=datetime(2026, 1, 10, 9, 30, tzinfo=timezone.utc),
        status="in_progress",
        checked_out_at=datetime(
            2026,
            1,
            10,
            9,
            40,
            0,
            500000,
            tzinfo=timezone.utc,
        ),
        phone="confirmed-in-progress",
    )
    excluded = [
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 10, 11, tzinfo=timezone.utc),
            status="completed",
            phone="completed-without-checkout",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 10, 12, tzinfo=timezone.utc),
            status="in_progress",
            phone="in-progress",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 10, 13, tzinfo=timezone.utc),
            status="cancelled",
            checked_out_at=datetime(2026, 1, 10, 13, 5, tzinfo=timezone.utc),
            phone="cancelled",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 10, 13, 30, tzinfo=timezone.utc),
            status="timeout",
            checked_out_at=datetime(2026, 1, 10, 13, 35, tzinfo=timezone.utc),
            phone="timeout",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 9, 23, 59, 59, tzinfo=timezone.utc),
            status="completed",
            checked_out_at=datetime(2026, 1, 10, 1, tzinfo=timezone.utc),
            phone="entered-before-window",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 11, tzinfo=timezone.utc),
            phone="end-is-exclusive",
        ),
        _handoff(
            other_tenant.id,
            other_session.id,
            created_at=datetime(2026, 1, 10, 14, tzinfo=timezone.utc),
            phone="other-tenant",
        ),
    ]
    _seed(
        client,
        tenant,
        other_tenant,
        session,
        other_session,
        pending,
        accepted,
        completed,
        confirmed_in_progress,
        *excluded,
    )

    response = _export(
        client,
        tenant.id,
        from_date="2026-01-10",
        to_date="2026-01-10",
    )

    assert response.status_code == 200, response.text
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == (
        'attachment; filename="handoffs_2026-01-10_2026-01-10.csv"'
    )
    _, rows = _csv_rows(response)
    assert [row["numero_whatsapp"] for row in rows] == [
        "accepted",
        "confirmed-in-progress",
        "completed-after-window",
        "pending",
    ]
    assert [row["status"] for row in rows] == [
        "accepted",
        "in_progress",
        "completed",
        "pending",
    ]
    assert rows[0]["tempo_espera_segundos"] == "180000.0"
    assert rows[1]["tempo_espera_segundos"] == "600.5"
    assert rows[2]["tempo_espera_segundos"] == "86430.3"
    assert rows[3]["tempo_espera_segundos"] == "187199.6"
    assert rows[0]["data_hora_saida"] == ""
    assert rows[3]["data_hora_saida"] == ""
    assert rows[1]["data_hora_saida"] == "2026-01-10T09:40:00.500000+00:00"
    assert rows[2]["data_hora_saida"] == "2026-01-11T09:00:30.260000+00:00"
    assert all(row["created_at"].endswith("+00:00") for row in rows)
    assert {row["exported_at"] for row in rows} == {"2026-01-12T12:00:00+00:00"}
    assert now_calls == 1
    assert selected_columns == [
        "numero_whatsapp",
        "session_id",
        "created_at",
        "data_hora_saida",
        "status",
    ]


def test_export_uses_clinic_timezone_fallback_and_ignores_browser_timezone(
    client: TestClient,
) -> None:
    tenant = _tenant(timezone_name=None)
    session = _session(tenant.id)
    records = [
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 10, 2, 59, 59, tzinfo=timezone.utc),
            phone="before-local-day",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 10, 3, tzinfo=timezone.utc),
            phone="local-start",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 11, 2, 59, 59, tzinfo=timezone.utc),
            phone="local-end-minus-one",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 1, 11, 3, tzinfo=timezone.utc),
            phone="next-local-day",
        ),
    ]
    _seed(client, tenant, session, *records)

    response = _export(
        client,
        tenant.id,
        from_date="2026-01-10",
        to_date="2026-01-10",
        timezone="Asia/Tokyo",
    )
    invalid_browser_timezone = _export(
        client,
        tenant.id,
        from_date="2026-01-10",
        to_date="2026-01-10",
        timezone="Mars/Olympus_Mons",
    )

    assert response.status_code == 200
    _, rows = _csv_rows(response)
    assert [row["numero_whatsapp"] for row in rows] == [
        "local-end-minus-one",
        "local-start",
    ]
    assert invalid_browser_timezone.status_code == 200
    _, invalid_rows = _csv_rows(invalid_browser_timezone)
    assert [row["numero_whatsapp"] for row in invalid_rows] == [
        "local-end-minus-one",
        "local-start",
    ]


def test_export_queue_and_radar_share_clinic_dst_cohort(
    client: TestClient,
) -> None:
    tenant = _tenant(timezone_name="America/New_York")
    before_session = _session(tenant.id)
    start_session = _session(tenant.id)
    end_minus_session = _session(tenant.id)
    end_session = _session(tenant.id)
    records = [
        _handoff(
            tenant.id,
            before_session.id,
            created_at=datetime(2026, 3, 8, 4, 59, 59, tzinfo=timezone.utc),
            phone="before-dst-day",
        ),
        _handoff(
            tenant.id,
            start_session.id,
            created_at=datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
            phone="dst-start",
        ),
        _handoff(
            tenant.id,
            end_minus_session.id,
            created_at=datetime(2026, 3, 9, 3, 59, 59, tzinfo=timezone.utc),
            phone="dst-end-minus-one",
        ),
        _handoff(
            tenant.id,
            end_session.id,
            created_at=datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
            phone="dst-end-exclusive",
        ),
    ]
    _seed(
        client,
        tenant,
        before_session,
        start_session,
        end_minus_session,
        end_session,
        *records,
    )
    params = {
        "tenant_id": str(tenant.id),
        "from_date": "2026-03-08",
        "to_date": "2026-03-08",
        "timezone": "Asia/Tokyo",
    }
    headers = _jwt_headers("clinic_admin", tenant.id)

    queue = client.get("/v1/handoffs/queue", params=params, headers=headers)
    exported = client.get(
        "/v1/handoffs/queue/export",
        params=params,
        headers=headers,
    )

    assert queue.status_code == 200, queue.text
    assert exported.status_code == 200, exported.text
    queue_body = queue.json()
    assert queue_body["period_timezone"] == "America/New_York"
    assert queue_body["period_start"] == "2026-03-08T05:00:00Z"
    assert queue_body["period_end_exclusive"] == "2026-03-09T04:00:00Z"
    queue_session_ids = {item["session_id"] for item in queue_body["items"]}
    _, export_rows = _csv_rows(exported)
    export_session_ids = {row["session_id"] for row in export_rows}
    assert (
        queue_session_ids
        == export_session_ids
        == {
            str(start_session.id),
            str(end_minus_session.id),
        }
    )

    export_parameters = client.get("/openapi.json").json()["paths"][
        "/v1/handoffs/queue/export"
    ]["get"]["parameters"]
    assert "timezone" not in {parameter["name"] for parameter in export_parameters}


@pytest.mark.parametrize(
    ("boundary_date", "timezone_name"),
    [
        ("9999-12-31", "UTC"),
        ("0001-01-01", "Asia/Tokyo"),
    ],
)
def test_export_rejects_unrepresentable_utc_windows_before_querying_handoffs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    boundary_date: str,
    timezone_name: str,
) -> None:
    tenant = _tenant(timezone_name=timezone_name)
    _seed(client, tenant)
    query_calls = 0
    original_execute = AsyncSession.execute

    async def unexpected_execute(self: AsyncSession, *args: Any, **kwargs: Any) -> Any:
        nonlocal query_calls
        if "handoff_records" in str(args[0]):
            query_calls += 1
            pytest.fail("handoff query must not run for an invalid date range")
        return await original_execute(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", unexpected_execute)

    response = _export(
        client,
        tenant.id,
        from_date=boundary_date,
        to_date=boundary_date,
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "invalid_date_range"}
    assert not response.content.startswith(b"\xef\xbb\xbf")
    assert query_calls == 0


def test_export_uses_configured_utc_clinic_timezone(client: TestClient) -> None:
    tenant = _tenant()
    session = _session(tenant.id)
    at_utc_start = _handoff(
        tenant.id,
        session.id,
        created_at=datetime(2026, 2, 3, tzinfo=timezone.utc),
        phone="utc-start",
    )
    before_utc_start = _handoff(
        tenant.id,
        session.id,
        created_at=datetime(2026, 2, 2, 23, 59, 59, tzinfo=timezone.utc),
        phone="before-utc-start",
    )
    _seed(client, tenant, session, at_utc_start, before_utc_start)

    response = _export(
        client,
        tenant.id,
        from_date="2026-02-03",
        to_date="2026-02-03",
    )

    assert response.status_code == 200
    _, rows = _csv_rows(response)
    assert [row["numero_whatsapp"] for row in rows] == ["utc-start"]


def test_export_escapes_only_dangerous_whatsapp_csv_prefixes(
    client: TestClient,
) -> None:
    tenant = _tenant()
    session = _session(tenant.id)
    phones = [
        "=SUM(A1:A2)",
        "+5511999999999",
        "-10+20",
        "@danger",
        "\tformula",
        "\rformula",
        "\nformula",
        "＝SUM(A1:A2)",
        "＋5511999999999",
        "－10+20",
        "＠danger",
        "5511999999999",
    ]
    records = [
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 2, 3, 12, index, tzinfo=timezone.utc),
            phone=phone,
        )
        for index, phone in enumerate(phones)
    ]
    _seed(client, tenant, session, *records)

    response = _export(
        client,
        tenant.id,
        from_date="2026-02-03",
        to_date="2026-02-03",
    )

    assert response.status_code == 200
    _, rows = _csv_rows(response)
    exported_phones = {row["numero_whatsapp"] for row in rows}
    assert exported_phones == {
        "'=SUM(A1:A2)",
        "'+5511999999999",
        "'-10+20",
        "'@danger",
        "'\tformula",
        "'\rformula",
        "'\nformula",
        "'＝SUM(A1:A2)",
        "'＋5511999999999",
        "'－10+20",
        "'＠danger",
        "5511999999999",
    }
    assert [record.numero_whatsapp for record in records] == phones


def test_export_preserves_legacy_partial_date_defaults(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import handoff_service

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            value = cls(2026, 5, 31, 12, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(handoff_service, "datetime", FrozenDatetime)
    tenant = _tenant()
    session = _session(tenant.id)
    records = [
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            phone="default-from-boundary",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
            phone="shared",
        ),
        _handoff(
            tenant.id,
            session.id,
            created_at=datetime(2026, 5, 31, 23, 59, 59, tzinfo=timezone.utc),
            phone="default-to-boundary",
        ),
    ]
    _seed(client, tenant, session, *records)

    only_from = _export(client, tenant.id, from_date="2026-05-15")
    only_to = _export(client, tenant.id, to_date="2026-05-15")

    assert only_from.status_code == 200
    assert only_from.headers["content-disposition"] == (
        'attachment; filename="handoffs_2026-05-15_2026-05-31.csv"'
    )
    _, from_rows = _csv_rows(only_from)
    assert [row["numero_whatsapp"] for row in from_rows] == [
        "default-to-boundary",
        "shared",
    ]
    assert only_to.status_code == 200
    assert only_to.headers["content-disposition"] == (
        'attachment; filename="handoffs_2026-05-01_2026-05-15.csv"'
    )
    _, to_rows = _csv_rows(only_to)
    assert [row["numero_whatsapp"] for row in to_rows] == [
        "shared",
        "default-from-boundary",
    ]


def test_export_enforces_ten_thousand_limit_without_partial_csv(
    client: TestClient,
) -> None:
    tenant = _tenant()
    session = _session(tenant.id)
    created_at = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    records = []
    for index in range(10001):
        record = _handoff(
            tenant.id,
            session.id,
            created_at=(
                created_at
                if index < 10000
                else datetime(2026, 3, 2, 12, tzinfo=timezone.utc)
            ),
        )
        record.id = UUID(f"aaaaaaaa-0000-4000-8000-{index:012x}")
        records.append(record)
    _seed(client, tenant, session)
    _seed_in_batches(client, records)

    accepted = _export(
        client,
        tenant.id,
        from_date="2026-03-01",
        to_date="2026-03-01",
    )

    response = _export(
        client,
        tenant.id,
        from_date="2026-03-01",
        to_date="2026-03-02",
    )

    assert accepted.status_code == 200
    _, accepted_rows = _csv_rows(accepted)
    assert len(accepted_rows) == 10000
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "export_limit_10000_exceeded"}
    assert not response.content.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize(
    ("status", "created_at", "checked_out_at"),
    [
        (
            "pending",
            datetime(2026, 6, 1, 13, tzinfo=timezone.utc),
            None,
        ),
        (
            "in_progress",
            datetime(2026, 6, 1, 11, tzinfo=timezone.utc),
            datetime(2026, 6, 1, 10, tzinfo=timezone.utc),
        ),
    ],
)
def test_export_fails_before_csv_for_invalid_temporal_invariants(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    created_at: datetime,
    checked_out_at: datetime | None,
) -> None:
    from src.api.routes import handoff as route

    class ExportDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return datetime(2026, 6, 1, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(route, "datetime", ExportDatetime)
    tenant = _tenant()
    session = _session(tenant.id)
    record = _handoff(
        tenant.id,
        session.id,
        created_at=created_at,
        status=status,
        checked_out_at=checked_out_at,
        phone="invalid-temporal-order",
    )
    _seed(client, tenant, session, record)

    response = _export(
        client,
        tenant.id,
        from_date="2026-06-01",
        to_date="2026-06-01",
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "invalid_handoff_timestamps"}
    assert "content-disposition" not in response.headers
    assert not response.content.startswith(b"\xef\xbb\xbf")


def test_export_requires_credentials_and_honors_api_key_tenant_boundary(
    client: TestClient,
) -> None:
    api_key = "handoff-export-api-key"
    owner = _tenant()
    owner.api_key_hash = pwd_context.hash(api_key)
    outsider = _tenant()
    session = _session(owner.id)
    record = _handoff(
        owner.id,
        session.id,
        created_at=datetime(2026, 4, 2, 12, tzinfo=timezone.utc),
        phone="api-key-authorized",
    )
    _seed(client, owner, outsider, session, record)
    params = {
        "tenant_id": str(owner.id),
        "from_date": "2026-04-02",
        "to_date": "2026-04-02",
    }

    missing = client.get("/v1/handoffs/queue/export", params=params)
    own = client.get(
        "/v1/handoffs/queue/export",
        params=params,
        headers={"X-API-Key": api_key},
    )
    cross_tenant = client.get(
        "/v1/handoffs/queue/export",
        params={**params, "tenant_id": str(outsider.id)},
        headers={"X-API-Key": api_key},
    )

    assert missing.status_code == 401
    assert own.status_code == 200
    _, rows = _csv_rows(own)
    assert [row["numero_whatsapp"] for row in rows] == ["api-key-authorized"]
    assert cross_tenant.status_code == 403
    assert cross_tenant.json() == {"detail": "tenant_mismatch"}


def test_export_preserves_clinic_builder_super_admin_and_tenant_boundaries(
    client: TestClient,
) -> None:
    builder_id = uuid4()
    builder = Builder(id=builder_id, name="Export Builder")
    clinic = _tenant(builder_id=builder_id)
    outsider = _tenant()
    session = _session(clinic.id)
    record = _handoff(
        clinic.id,
        session.id,
        created_at=datetime(2026, 4, 1, 12, tzinfo=timezone.utc),
        phone="authorized",
    )
    _seed(client, builder, clinic, outsider, session, record)
    params = {"from_date": "2026-04-01", "to_date": "2026-04-01"}

    own = _export(client, clinic.id, **params)
    builder_response = _export(
        client,
        clinic.id,
        headers=_jwt_headers("builder", uuid4(), builder_id=builder_id),
        **params,
    )
    super_admin = _export(
        client,
        clinic.id,
        headers=_jwt_headers("super_admin", uuid4()),
        **params,
    )
    rejected = _export(
        client,
        clinic.id,
        headers=_jwt_headers("clinic_admin", outsider.id),
        **params,
    )

    assert own.status_code == 200
    assert builder_response.status_code == 200
    assert super_admin.status_code == 200
    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "tenant_mismatch"}
    for response in (own, builder_response, super_admin):
        _, rows = _csv_rows(response)
        assert [row["numero_whatsapp"] for row in rows] == ["authorized"]
