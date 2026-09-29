"""Contract tests for handoff queue projection and checkout."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.settings import settings
from src.handoff.models import HandoffReason


def _jwt_headers(role: str, tenant_id: UUID) -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": role,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _session(tenant_id: UUID, user_id: str) -> Any:
    from src.models.session import Session

    return Session(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        channel="whatsapp",
        cognitive_state={},
        session_metadata={},
        last_activity_at=datetime.now(timezone.utc),
        is_active=True,
        is_paused=True,
    )


def _record(
    tenant_id: UUID,
    session_id: UUID,
    *,
    created_at: datetime,
    reason: str,
    intent: str | None,
    context: dict[str, Any],
    first_message: str,
    status: str = "pending",
    confirmed_at: datetime | None = None,
    sla_target_seconds: int | None = 600,
) -> Any:
    from src.models.handoff import HandoffRecord

    return HandoffRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        reason=reason,
        summary="queue contract",
        context=context,
        status=status,
        urgency="normal",
        numero_whatsapp="5511999999999",
        intencao_detectada=intent,
        estagio_atual="em_qualificacao",
        primeira_linha_mensagem=first_message,
        data_hora_saida=confirmed_at,
        sla_target_seconds=sla_target_seconds,
        created_at=created_at,
    )


def _seed(
    client: TestClient,
    *,
    tenants: list[UUID],
    sessions: list[Any],
    handoffs: list[Any],
    clinic_timezones: dict[UUID, str] | None = None,
) -> None:
    from src.models.clinic_config_table import ClinicConfigTable
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add_all(
                [
                    Tenant(
                        id=tenant_id,
                        name=f"Clinic {tenant_id}",
                        slug=f"clinic-{tenant_id}",
                        api_key_hash=f"hash-{tenant_id}",
                        config={},
                    )
                    for tenant_id in tenants
                ]
            )
            db.add_all(sessions)
            db.add_all(
                [
                    ClinicConfigTable(
                        tenant_id=tenant_id,
                        operations={"timezone": timezone_name},
                    )
                    for tenant_id, timezone_name in (clinic_timezones or {}).items()
                ]
            )
            db.add_all(handoffs)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _handoff_state(client: TestClient, handoff_id: UUID) -> tuple[str, Any]:
    from src.models.handoff import HandoffRecord
    from src.repositories.database import get_db_session

    async def _run() -> tuple[str, Any]:
        db = get_db_session()
        try:
            result = await db.execute(
                select(
                    HandoffRecord.status,
                    HandoffRecord.data_hora_saida,
                ).where(HandoffRecord.id == handoff_id)
            )
            row = result.one()
            return row.status, row.data_hora_saida
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


def test_queue_and_checkout_share_canonical_projection_and_fifo(
    client: TestClient,
) -> None:
    tenant_id = uuid4()
    session = _session(tenant_id, "queue-contract")
    now = datetime.now(timezone.utc)
    cases = [
        ("canonical-true", HandoffReason.USER_REQUEST.value, "real_intent", True, True),
        (
            "canonical-false",
            HandoffReason.SAFETY_CONCERN.value,
            "another_real_intent",
            False,
            False,
        ),
        (
            "absent",
            HandoffReason.SAFETY_CONCERN.value,
            HandoffReason.USER_REQUEST.value,
            None,
            True,
        ),
        ("none", HandoffReason.USER_REQUEST.value, "none_intent", None, False),
        (
            "string",
            HandoffReason.SAFETY_CONCERN.value,
            "string_intent",
            "false",
            True,
        ),
        ("number", HandoffReason.USER_REQUEST.value, "number_intent", 1, False),
        (
            "list",
            HandoffReason.SAFETY_CONCERN.value,
            "list_intent",
            [False],
            True,
        ),
        (
            "map",
            HandoffReason.USER_REQUEST.value,
            "map_intent",
            {"value": True},
            False,
        ),
    ]
    records = []
    expected: dict[str, tuple[bool, str | None, str]] = {}
    for index, (label, reason, intent, context_value, safety_flag) in enumerate(cases):
        context = {} if label == "absent" else {"safety_concern": context_value}
        first_message = f"private-{label}"
        record = _record(
            tenant_id,
            session.id,
            created_at=now + timedelta(seconds=index),
            reason=reason,
            intent=intent,
            context=context,
            first_message=first_message,
        )
        records.append(record)
        normalized_intent = (
            None if intent in {item.value for item in HandoffReason} else intent
        )
        expected[str(record.id)] = (safety_flag, normalized_intent, first_message)

    # Deliberately persist newest-first so response order can only pass via ORDER BY.
    _seed(
        client,
        tenants=[tenant_id],
        sessions=[session],
        handoffs=list(reversed(records)),
    )
    headers = _jwt_headers("clinic_admin", tenant_id)

    listed = client.get(
        "/v1/handoffs/queue",
        params={"tenant_id": str(tenant_id)},
        headers=headers,
    )

    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [item["id"] for item in items] == [str(record.id) for record in records]
    for index, item in enumerate(items):
        safety_flag, intent, _ = expected[item["id"]]
        assert item["safety_flag"] is safety_flag
        assert item["intencao_detectada"] == intent
        assert item["primeira_linha_mensagem"] is None
        assert item["sla_target_seconds"] == 600

        checked_out = client.post(
            (
                f"/v1/handoffs/queue/{item['id']}/confirm"
                if index == 0
                else f"/v1/handoffs/queue/{item['id']}/checkout"
            ),
            headers=headers,
        )
        assert checked_out.status_code == 200, checked_out.text
        checkout_body = checked_out.json()
        assert checkout_body["safety_flag"] is safety_flag
        assert checkout_body["intencao_detectada"] == intent
        assert checkout_body["primeira_linha_mensagem"] == expected[item["id"]][2]
        assert checkout_body["status"] == "in_progress"
        assert checkout_body["data_hora_saida"] is not None
        assert checkout_body["wait_seconds"] is not None

    duplicate = client.post(
        f"/v1/handoffs/queue/{records[0].id}/checkout",
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "attendance_already_confirmed"


def test_queue_preserves_authentication_and_tenant_boundaries(
    client: TestClient,
) -> None:
    own_tenant = uuid4()
    other_tenant = uuid4()
    own_session = _session(own_tenant, "own")
    other_session = _session(other_tenant, "other")
    created_at = datetime.now(timezone.utc)
    own = _record(
        own_tenant,
        own_session.id,
        created_at=created_at,
        reason=HandoffReason.USER_REQUEST.value,
        intent="own_intent",
        context={},
        first_message="own-private",
    )
    other = _record(
        other_tenant,
        other_session.id,
        created_at=created_at,
        reason=HandoffReason.SAFETY_CONCERN.value,
        intent="other_intent",
        context={},
        first_message="other-private",
    )
    _seed(
        client,
        tenants=[own_tenant, other_tenant],
        sessions=[own_session, other_session],
        handoffs=[other, own],
    )
    headers = _jwt_headers("clinic_admin", own_tenant)

    unauthenticated = client.get(
        "/v1/handoffs/queue",
        params={"tenant_id": str(own_tenant)},
    )
    own_queue = client.get(
        "/v1/handoffs/queue",
        params={"tenant_id": str(own_tenant)},
        headers=headers,
    )
    tenant_override = client.get(
        "/v1/handoffs/queue",
        params={"tenant_id": str(other_tenant)},
        headers=headers,
    )
    cross_tenant_checkout = client.post(
        f"/v1/handoffs/queue/{other.id}/checkout",
        params={"tenant_id": str(other_tenant)},
        headers=headers,
    )

    assert unauthenticated.status_code == 401
    assert own_queue.status_code == 200
    assert [item["id"] for item in own_queue.json()["items"]] == [str(own.id)]
    assert tenant_override.status_code == 403
    assert tenant_override.json()["detail"] == "tenant_mismatch"
    assert cross_tenant_checkout.status_code == 404
    assert cross_tenant_checkout.json()["detail"] == "handoff_not_found"
    assert _handoff_state(client, other.id) == ("pending", None)


def test_queue_radar_uses_one_cohort_snapshots_and_nullable_no_data(
    client: TestClient,
) -> None:
    tenant_id = uuid4()
    empty_tenant_id = uuid4()
    session = _session(tenant_id, "radar")
    empty_session = _session(empty_tenant_id, "empty-radar")
    now = datetime.now(timezone.utc)
    base = now - timedelta(minutes=20)

    def occurrence(
        label: str,
        *,
        created_at: datetime = base,
        status: str = "pending",
        confirmed_after: int | None = None,
        target: int | None = 600,
    ) -> Any:
        return _record(
            tenant_id,
            session.id,
            created_at=created_at,
            reason=HandoffReason.USER_REQUEST.value,
            intent=label,
            context={},
            first_message=f"private-{label}",
            status=status,
            confirmed_at=(
                created_at + timedelta(seconds=confirmed_after)
                if confirmed_after is not None
                else None
            ),
            sla_target_seconds=target,
        )

    records = [
        occurrence("within-599", status="completed", confirmed_after=599),
        occurrence("within-600", status="in_progress", confirmed_after=600),
        occurrence("late-601", status="completed", confirmed_after=601),
        occurrence("overdue-one"),
        occurrence("overdue-two"),
        occurrence("cancelled", status="cancelled"),
        occurrence("legacy", target=None),
        occurrence("maturing", created_at=now - timedelta(seconds=30)),
    ]
    period_from = min(record.created_at.date() for record in records)
    period_to = max(record.created_at.date() for record in records)
    outside = occurrence(
        "outside-end",
        created_at=datetime.combine(
            period_to + timedelta(days=1),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ),
    )
    _seed(
        client,
        tenants=[tenant_id, empty_tenant_id],
        sessions=[session, empty_session],
        handoffs=[*records, outside],
        clinic_timezones={tenant_id: "UTC", empty_tenant_id: "UTC"},
    )

    response = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": period_from.isoformat(),
            "to_date": period_to.isoformat(),
        },
        headers=_jwt_headers("clinic_admin", tenant_id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["avg_wait_seconds"] == 600.0
    assert body["pct_within_sla"] == 40.0
    assert body["within_sla_count"] == 2
    assert body["confirmed_late_count"] == 1
    assert body["unanswered_overdue_count"] == 2
    assert body["maturing_count"] == 1
    assert body["cancelled_unconfirmed_count"] == 1
    assert body["unmeasured_legacy_count"] == 1
    assert body["confirmed_count"] == 3
    assert body["sla_eligible_count"] == 5
    assert sum(
        body[key]
        for key in (
            "within_sla_count",
            "confirmed_late_count",
            "unanswered_overdue_count",
            "maturing_count",
            "cancelled_unconfirmed_count",
            "unmeasured_legacy_count",
        )
    ) == len(records)
    assert {item["intencao_detectada"] for item in body["items"]} == {
        "overdue-one",
        "overdue-two",
        "legacy",
        "maturing",
    }
    assert body["period_timezone"] == "UTC"
    assert body["period_start"].endswith(("Z", "+00:00"))
    assert body["period_end_exclusive"].endswith(("Z", "+00:00"))

    empty = client.get(
        "/v1/handoffs/queue",
        params={"tenant_id": str(empty_tenant_id)},
        headers=_jwt_headers("clinic_admin", empty_tenant_id),
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["avg_wait_seconds"] is None
    assert empty.json()["pct_within_sla"] is None
    assert empty.json()["confirmed_count"] == 0
    assert empty.json()["sla_eligible_count"] == 0


def test_queue_period_uses_clinic_timezone_default_window_and_dst(
    client: TestClient,
) -> None:
    tenant_id = uuid4()
    session = _session(tenant_id, "dst-radar")
    at_start = _record(
        tenant_id,
        session.id,
        created_at=datetime(2026, 3, 8, 5, tzinfo=timezone.utc),
        reason=HandoffReason.USER_REQUEST.value,
        intent="at-start",
        context={},
        first_message="private-at-start",
    )
    at_end = _record(
        tenant_id,
        session.id,
        created_at=datetime(2026, 3, 9, 4, tzinfo=timezone.utc),
        reason=HandoffReason.USER_REQUEST.value,
        intent="at-end",
        context={},
        first_message="private-at-end",
    )
    _seed(
        client,
        tenants=[tenant_id],
        sessions=[session],
        handoffs=[at_start, at_end],
        clinic_timezones={tenant_id: "America/New_York"},
    )

    response = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": "2026-03-08",
            "to_date": "2026-03-08",
            "timezone": "Asia/Tokyo",
        },
        headers=_jwt_headers("clinic_admin", tenant_id),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(at_start.id)]
    assert body["period_timezone"] == "America/New_York"
    assert body["period_start"] == "2026-03-08T05:00:00Z"
    assert body["period_end_exclusive"] == "2026-03-09T04:00:00Z"

    invalid = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": "2026-03-09",
            "to_date": "2026-03-08",
        },
        headers=_jwt_headers("clinic_admin", tenant_id),
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid_date_range"

    from src.services.handoff_service import HandoffService

    fixed_as_of = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    default_period = client._submit(  # type: ignore[attr-defined]
        HandoffService().get_queue_sla(tenant_id, as_of=fixed_as_of)
    )
    assert default_period.period.start_utc == datetime(
        2026, 8, 29, 4, tzinfo=timezone.utc
    )
    assert default_period.period.end_exclusive_utc == datetime(
        2026, 9, 29, 4, tzinfo=timezone.utc
    )


def test_queue_rejects_unrepresentable_local_start_before_cohort_query(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.repositories.handoff import HandoffRecordRepository

    tenant_id = uuid4()
    session = _session(tenant_id, "date-min-radar")
    _seed(
        client,
        tenants=[tenant_id],
        sessions=[session],
        handoffs=[],
        clinic_timezones={tenant_id: "Asia/Tokyo"},
    )
    cohort_query = AsyncMock(
        side_effect=AssertionError("invalid period must not query the cohort")
    )
    monkeypatch.setattr(
        HandoffRecordRepository,
        "find_sla_cohort",
        cohort_query,
    )

    response = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": "0001-01-01",
            "to_date": "0001-01-01",
        },
        headers=_jwt_headers("clinic_admin", tenant_id),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_date_range"}
    cohort_query.assert_not_awaited()


def test_confirmation_is_required_for_patch_and_completion(
    client: TestClient,
) -> None:
    tenant_id = uuid4()
    session = _session(tenant_id, "confirmation-required")
    record = _record(
        tenant_id,
        session.id,
        created_at=datetime.now(timezone.utc),
        reason=HandoffReason.USER_REQUEST.value,
        intent="confirmation-required",
        context={},
        first_message="private",
    )
    _seed(
        client,
        tenants=[tenant_id],
        sessions=[session],
        handoffs=[record],
    )
    headers = _jwt_headers("clinic_admin", tenant_id)

    patched = client.patch(
        f"/v1/handoffs/{record.id}",
        json={"status": "in_progress"},
        headers=headers,
    )
    completed = client.post(
        f"/v1/handoffs/{record.id}/complete",
        headers=headers,
    )

    assert patched.status_code == 409
    assert patched.json()["detail"] == "attendance_confirmation_required"
    assert completed.status_code == 409
    assert completed.json()["detail"] == "attendance_not_confirmed"
    assert _handoff_state(client, record.id) == ("pending", None)


def test_openapi_exposes_required_nullable_sla_contract(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    schemas = document["components"]["schemas"]
    queue_schema = schemas["HandoffQueueResponse"]
    item_schema = schemas["HandoffQueueItem"]

    assert {
        "avg_wait_seconds",
        "pct_within_sla",
        "within_sla_count",
        "confirmed_late_count",
        "unanswered_overdue_count",
        "maturing_count",
        "cancelled_unconfirmed_count",
        "unmeasured_legacy_count",
        "confirmed_count",
        "sla_eligible_count",
        "period_timezone",
        "period_start",
        "period_end_exclusive",
    } <= set(queue_schema["required"])
    assert "sla_target_seconds" in item_schema["required"]
    assert {
        branch.get("type")
        for branch in queue_schema["properties"]["avg_wait_seconds"]["anyOf"]
    } == {
        "number",
        "null",
    }
    assert {
        branch.get("type")
        for branch in item_schema["properties"]["sla_target_seconds"]["anyOf"]
    } == {
        "integer",
        "null",
    }
    assert "lifecycle_version" not in str(queue_schema)
    assert "lifecycle_version" not in str(item_schema)

    confirm = document["paths"]["/v1/handoffs/queue/{handoff_id}/confirm"]["post"]
    checkout = document["paths"]["/v1/handoffs/queue/{handoff_id}/checkout"]["post"]
    assert confirm.get("deprecated") is not True
    assert checkout["deprecated"] is True
    assert "409" in confirm["responses"]
    assert "409" in document["paths"]["/v1/handoffs/{handoff_id}"]["patch"]["responses"]
    assert (
        "409"
        in document["paths"]["/v1/handoffs/{handoff_id}/complete"]["post"]["responses"]
    )
