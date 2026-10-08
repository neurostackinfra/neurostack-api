"""Contract tests for handoff queue projection and checkout."""

from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.settings import settings
from src.handoff.models import HandoffReason
from src.services.handoff_service import (
    HandoffConflictError,
    HandoffNotFoundError,
    HandoffPersistenceError,
    HandoffPostCommitError,
    HandoffTransitionError,
)


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


def _seed_entities(client: TestClient, *entities: Any) -> None:
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add_all(entities)
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


def test_queue_confirmation_routes_preserve_distinct_contracts_and_fifo(
    client: TestClient,
) -> None:
    tenant_id = uuid4()
    session = _session(tenant_id, "queue-contract")
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
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
        response_body = checked_out.json()
        assert response_body["status"] == "in_progress"
        assert response_body["data_hora_saida"] is not None
        if index == 0:
            assert response_body["tenant_id"] == str(tenant_id)
            assert response_body["summary"] == "queue contract"
            assert "wait_seconds" not in response_body
            assert "primeira_linha_mensagem" not in response_body
        else:
            assert response_body["safety_flag"] is safety_flag
            assert response_body["intencao_detectada"] == intent
            assert response_body["primeira_linha_mensagem"] == expected[item["id"]][2]
            assert response_body["wait_seconds"] is not None
            assert "tenant_id" not in response_body
            assert "summary" not in response_body

    duplicate = client.post(
        f"/v1/handoffs/queue/{records[0].id}/checkout",
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "handoff_conflict"


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
        headers=headers,
    )

    assert unauthenticated.status_code == 401
    assert own_queue.status_code == 200
    assert [item["id"] for item in own_queue.json()["items"]] == [str(own.id)]
    assert tenant_override.status_code == 403
    assert tenant_override.json() == {"detail": {"code": "tenant_override_forbidden"}}
    assert cross_tenant_checkout.status_code == 404
    assert cross_tenant_checkout.json()["detail"] == "handoff_not_found"
    assert _handoff_state(client, other.id) == ("pending", None)


def test_queue_uses_canonical_deny_by_default_tenant_resolution(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.middleware.auth import APIKeyStore, TenantContext
    from src.models.builder import Builder
    from src.models.tenant import Tenant

    builder_id = uuid4()
    clinic_id = uuid4()
    outsider_id = uuid4()
    token_tenant_id = uuid4()
    builder = Builder(id=builder_id, name="Queue Builder")
    clinic = Tenant(
        id=clinic_id,
        name="Queue Clinic",
        slug=f"queue-clinic-{clinic_id}",
        api_key_hash=f"queue-hash-{clinic_id}",
        builder_id=builder_id,
        config={},
    )
    outsider = Tenant(
        id=outsider_id,
        name="Queue Outsider",
        slug=f"queue-outsider-{outsider_id}",
        api_key_hash=f"queue-hash-{outsider_id}",
        config={},
    )
    session = _session(clinic_id, "canonical-auth")
    secret_phone = "5511-secret-pii"
    record = _record(
        clinic_id,
        session.id,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        reason=HandoffReason.USER_REQUEST.value,
        intent="canonical-auth",
        context={},
        first_message="private-auth-message",
    )
    record.numero_whatsapp = secret_phone
    _seed_entities(client, builder, clinic, outsider, session, record)

    api_key = "queue-canonical-api-key"

    async def accept_api_key(_cls: type[APIKeyStore], value: str):
        if value != api_key:
            return None
        return TenantContext(tenant_id=clinic_id, api_key=value)

    monkeypatch.setattr(
        APIKeyStore,
        "validate_api_key",
        classmethod(accept_api_key),
    )

    requests = {
        "api_key": client.get(
            "/v1/handoffs/queue",
            params={"tenant_id": str(clinic_id)},
            headers={"X-API-Key": api_key},
        ),
        "clinic_admin": client.get(
            "/v1/handoffs/queue",
            params={"tenant_id": str(clinic_id)},
            headers=_jwt_headers("clinic_admin", clinic_id),
        ),
        "builder": client.get(
            "/v1/handoffs/queue",
            params={"tenant_id": str(clinic_id)},
            headers=_jwt_headers(
                "builder",
                token_tenant_id,
                builder_id=builder_id,
            ),
        ),
        "super_admin": client.get(
            "/v1/handoffs/queue",
            params={"tenant_id": str(clinic_id)},
            headers=_jwt_headers("super_admin", token_tenant_id),
        ),
    }
    for caller, response in requests.items():
        assert response.status_code == 200, (caller, response.text)
        assert [item["id"] for item in response.json()["items"]] == [str(record.id)]

    denied = [
        client.get(
            "/v1/handoffs/queue",
            params={"tenant_id": str(clinic_id)},
            headers=_jwt_headers(role, clinic_id),
        )
        for role in ("investor", "unknown")
    ]
    denied.append(
        client.get(
            "/v1/handoffs/queue",
            params={"tenant_id": str(outsider_id)},
            headers={"X-API-Key": api_key},
        )
    )
    for response in denied[:2]:
        assert response.status_code == 403
        assert response.json() == {"detail": {"code": "caller_role_forbidden"}}
        assert secret_phone not in response.text
    assert denied[2].status_code == 403
    assert denied[2].json() == {"detail": {"code": "tenant_override_forbidden"}}
    assert secret_phone not in denied[2].text


def test_checkout_resolves_clinic_builder_and_super_admin_selected_tenants(
    client: TestClient,
) -> None:
    from src.models.builder import Builder
    from src.models.tenant import Tenant

    builder_id = uuid4()
    builder = Builder(id=builder_id, name="Checkout Builder")
    clinic_id = uuid4()
    outsider_id = uuid4()
    clinic = Tenant(
        id=clinic_id,
        name="Managed Checkout Clinic",
        slug=f"checkout-clinic-{clinic_id}",
        api_key_hash=f"checkout-hash-{clinic_id}",
        builder_id=builder_id,
        config={},
    )
    outsider = Tenant(
        id=outsider_id,
        name="Unmanaged Checkout Clinic",
        slug=f"checkout-outsider-{outsider_id}",
        api_key_hash=f"checkout-hash-{outsider_id}",
        config={},
    )
    clinic_sessions = [
        _session(clinic_id, f"checkout-{role}")
        for role in ("clinic", "builder", "super-admin")
    ]
    outsider_session = _session(outsider_id, "checkout-outsider")
    now = datetime.now(timezone.utc)
    clinic_records = [
        _record(
            clinic_id,
            session.id,
            created_at=now - timedelta(minutes=index + 1),
            reason=HandoffReason.USER_REQUEST.value,
            intent="checkout",
            context={},
            first_message="selected clinic",
        )
        for index, session in enumerate(clinic_sessions)
    ]
    outsider_record = _record(
        outsider_id,
        outsider_session.id,
        created_at=now,
        reason=HandoffReason.USER_REQUEST.value,
        intent="checkout",
        context={},
        first_message="must remain pending",
    )
    _seed_entities(
        client,
        builder,
        clinic,
        outsider,
        *clinic_sessions,
        outsider_session,
        *clinic_records,
        outsider_record,
    )

    clinic_admin = client.post(
        f"/v1/handoffs/queue/{clinic_records[0].id}/checkout",
        params={"tenant_id": str(clinic_id)},
        headers=_jwt_headers("clinic_admin", clinic_id),
    )
    builder_response = client.post(
        f"/v1/handoffs/queue/{clinic_records[1].id}/checkout",
        params={"tenant_id": str(clinic_id)},
        headers=_jwt_headers("builder", uuid4(), builder_id=builder_id),
    )
    super_admin = client.post(
        f"/v1/handoffs/queue/{clinic_records[2].id}/checkout",
        params={"tenant_id": str(clinic_id)},
        headers=_jwt_headers("super_admin", uuid4()),
    )
    denied = client.post(
        f"/v1/handoffs/queue/{outsider_record.id}/checkout",
        params={"tenant_id": str(outsider_id)},
        headers=_jwt_headers("builder", uuid4(), builder_id=builder_id),
    )

    for response in (clinic_admin, builder_response, super_admin):
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "in_progress"
    assert denied.status_code == 403
    assert denied.json() == {"detail": {"code": "tenant_override_forbidden"}}
    assert _handoff_state(client, outsider_record.id) == ("pending", None)


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
    for timestamp in (
        body["period_start"],
        body["period_end_exclusive"],
        body["as_of"],
    ):
        assert timestamp.endswith(("Z", "+00:00"))

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


def test_queue_period_uses_clinic_timezone_defaults_partials_and_dst(
    client: TestClient,
) -> None:
    from src.services.handoff_service import HandoffService, HandoffSlaPeriodError

    tenant_id = uuid4()
    fallback_tenant_id = uuid4()
    session = _session(tenant_id, "dst-radar")
    fallback_session = _session(fallback_tenant_id, "fallback-radar")
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
        tenants=[tenant_id, fallback_tenant_id],
        sessions=[session, fallback_session],
        handoffs=[at_start, at_end],
        clinic_timezones={
            tenant_id: "America/New_York",
            fallback_tenant_id: "Not/A_Real_Zone",
        },
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

    service = HandoffService()
    fixed_as_of = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    default_period = client._submit(  # type: ignore[attr-defined]
        service.get_queue_sla(tenant_id, as_of=fixed_as_of)
    )
    assert default_period.period.start_date == date(2026, 8, 29)
    assert default_period.period.end_date == date(2026, 9, 28)
    assert (
        default_period.period.end_date - default_period.period.start_date
    ).days == 30

    from_only = client._submit(  # type: ignore[attr-defined]
        service.get_queue_sla(
            tenant_id,
            from_date=date(2026, 9, 1),
            as_of=fixed_as_of,
        )
    )
    assert from_only.period.start_date == date(2026, 9, 1)
    assert from_only.period.end_date == date(2026, 9, 28)

    to_only = client._submit(  # type: ignore[attr-defined]
        service.get_queue_sla(
            tenant_id,
            to_date=date(2026, 9, 10),
            as_of=fixed_as_of,
        )
    )
    assert to_only.period.start_date == date(2026, 8, 29)
    assert to_only.period.end_date == date(2026, 9, 10)

    with pytest.raises(HandoffSlaPeriodError, match="invalid_date_range"):
        client._submit(  # type: ignore[attr-defined]
            service.get_queue_sla(
                tenant_id,
                from_date=date(2026, 9, 29),
                as_of=fixed_as_of,
            )
        )

    fallback = client._submit(  # type: ignore[attr-defined]
        service.get_queue_sla(
            fallback_tenant_id,
            from_date=date(2026, 9, 28),
            to_date=date(2026, 9, 28),
            as_of=fixed_as_of,
        )
    )
    assert fallback.period.timezone_name == "America/Sao_Paulo"
    assert fallback.period.start_utc == datetime(2026, 9, 28, 3, tzinfo=timezone.utc)


def test_queue_allows_90_elapsed_date_days_and_rejects_91_with_typed_code(
    client: TestClient,
) -> None:
    from src.services.handoff_service import HandoffService, HandoffSlaPeriodError

    tenant_id = uuid4()
    session = _session(tenant_id, "bounded-radar")
    _seed(
        client,
        tenants=[tenant_id],
        sessions=[session],
        handoffs=[],
        clinic_timezones={tenant_id: "UTC"},
    )
    headers = _jwt_headers("clinic_admin", tenant_id)
    accepted = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": "2026-01-01",
            "to_date": "2026-04-01",
        },
        headers=headers,
    )
    rejected = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": "2026-01-01",
            "to_date": "2026-04-02",
        },
        headers=headers,
    )

    assert accepted.status_code == 200, accepted.text
    assert rejected.status_code == 400
    assert rejected.json() == {"detail": "max_window_90_days"}

    service = HandoffService()
    with pytest.raises(HandoffSlaPeriodError) as error:
        client._submit(  # type: ignore[attr-defined]
            service.get_queue_sla(
                tenant_id,
                from_date=date(2026, 1, 1),
                to_date=date(2026, 4, 2),
                as_of=datetime(2026, 4, 2, tzinfo=timezone.utc),
            )
        )
    assert error.value.code == "max_window_90_days"


def test_queue_ignores_export_lookback_without_addon(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import handoff_service

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            value = cls(2026, 10, 7, 12, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(handoff_service, "datetime", FrozenDatetime)
    tenant_id = uuid4()
    session = _session(tenant_id, "queue-ignores-export-lookback")
    _seed(
        client,
        tenants=[tenant_id],
        sessions=[session],
        handoffs=[
            _record(
                tenant_id,
                session.id,
                created_at=datetime(2026, 3, 10, 12, tzinfo=timezone.utc),
                reason=HandoffReason.USER_REQUEST.value,
                intent=None,
                context={},
                first_message="seven months old",
            )
        ],
        clinic_timezones={tenant_id: "UTC"},
    )

    # Seven months before the frozen clinic today, within the queue's 90 days;
    # the 6-month lookback belongs to the CSV exports only.
    response = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": "2026-03-07",
            "to_date": "2026-06-04",
        },
        headers=_jwt_headers("clinic_admin", tenant_id),
    )

    assert response.status_code == 200, response.text
    assert [item["session_id"] for item in response.json()["items"]] == [
        str(session.id)
    ]


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
    cohort_was_queried = False

    async def forbidden_cohort_query(*_args: object, **_kwargs: object):
        nonlocal cohort_was_queried
        cohort_was_queried = True
        raise AssertionError("invalid period must not query the cohort")
        yield  # pragma: no cover - marks this replacement as an async iterator

    monkeypatch.setattr(
        HandoffRecordRepository,
        "stream_sla_cohort",
        forbidden_cohort_query,
    )

    for from_date, to_date in (
        ("0001-01-01", "0001-01-01"),
        ("9999-12-31", "9999-12-31"),
    ):
        response = client.get(
            "/v1/handoffs/queue",
            params={
                "tenant_id": str(tenant_id),
                "from_date": from_date,
                "to_date": to_date,
            },
            headers=_jwt_headers("clinic_admin", tenant_id),
        )

        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_date_range"}
    assert cohort_was_queried is False


def test_queue_rejects_corrupt_persisted_timestamps_without_partial_metrics(
    client: TestClient,
) -> None:
    tenant_id = uuid4()
    session = _session(tenant_id, "corrupt-radar")
    created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    corrupt = _record(
        tenant_id,
        session.id,
        created_at=created_at,
        reason=HandoffReason.USER_REQUEST.value,
        intent="must-not-be-aggregated",
        context={},
        first_message="private-corrupt",
        status="completed",
        confirmed_at=created_at - timedelta(seconds=1),
        sla_target_seconds=600,
    )
    _seed(
        client,
        tenants=[tenant_id],
        sessions=[session],
        handoffs=[corrupt],
        clinic_timezones={tenant_id: "UTC"},
    )

    response = client.get(
        "/v1/handoffs/queue",
        params={
            "tenant_id": str(tenant_id),
            "from_date": created_at.date().isoformat(),
            "to_date": created_at.date().isoformat(),
        },
        headers=_jwt_headers("clinic_admin", tenant_id),
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "invalid_handoff_timestamps"}
    assert "avg_wait_seconds" not in response.json()
    assert "private-corrupt" not in response.text


def test_management_http_uses_tenant_scoped_lifecycle_and_stable_conflicts(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_store = MagicMock()
    event_store.append = AsyncMock()
    event_store.get_recent_messages = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "src.memory.event_store.get_event_store",
        AsyncMock(return_value=event_store),
    )
    own_tenant = uuid4()
    other_tenant = uuid4()
    own_session = _session(own_tenant, "management-own")
    other_session = _session(other_tenant, "management-other")
    created_at = datetime.now(timezone.utc)
    accepted = _record(
        own_tenant,
        own_session.id,
        created_at=created_at,
        reason=HandoffReason.USER_REQUEST.value,
        intent="accept",
        context={},
        first_message="own",
    )
    confirmed = _record(
        own_tenant,
        own_session.id,
        created_at=created_at + timedelta(seconds=1),
        reason=HandoffReason.USER_REQUEST.value,
        intent="confirm",
        context={},
        first_message="own-confirm",
    )
    other = _record(
        other_tenant,
        other_session.id,
        created_at=created_at,
        reason=HandoffReason.USER_REQUEST.value,
        intent="other",
        context={},
        first_message="other",
    )
    _seed(
        client,
        tenants=[own_tenant, other_tenant],
        sessions=[own_session, other_session],
        handoffs=[accepted, confirmed, other],
    )
    headers = _jwt_headers("clinic_admin", own_tenant)

    first_accept = client.post(
        f"/v1/handoffs/{accepted.id}/accept",
        headers=headers,
    )
    duplicate_accept = client.post(
        f"/v1/handoffs/{accepted.id}/accept",
        headers=headers,
    )
    completion_without_confirmation = client.post(
        f"/v1/handoffs/{accepted.id}/complete",
        headers=headers,
    )
    generic_confirmation = client.patch(
        f"/v1/handoffs/{confirmed.id}",
        headers=headers,
        json={"status": "in_progress"},
    )
    first_confirmation = client.post(
        f"/v1/handoffs/queue/{confirmed.id}/checkout",
        headers=headers,
    )
    duplicate_confirmation = client.post(
        f"/v1/handoffs/queue/{confirmed.id}/checkout",
        headers=headers,
    )
    completed_after_confirmation = client.post(
        f"/v1/handoffs/{confirmed.id}/complete",
        headers=headers,
    )
    cancelled_after_accept = client.post(
        f"/v1/handoffs/{accepted.id}/cancel",
        headers=headers,
    )
    cross_tenant_read = client.get(
        f"/v1/handoffs/{other.id}",
        headers=headers,
    )
    cross_tenant_cancel = client.post(
        f"/v1/handoffs/{other.id}/cancel",
        headers=headers,
    )

    assert first_accept.status_code == 200, first_accept.text
    assert duplicate_accept.status_code == 409
    assert duplicate_accept.json()["detail"] == "invalid_handoff_transition"
    assert completion_without_confirmation.status_code == 409
    assert completion_without_confirmation.json()["detail"] == (
        "invalid_handoff_transition"
    )
    assert generic_confirmation.status_code == 409
    assert generic_confirmation.json()["detail"] == "invalid_handoff_transition"
    assert first_confirmation.status_code == 200, first_confirmation.text
    assert first_confirmation.json()["status"] == "in_progress"
    assert duplicate_confirmation.status_code == 409
    assert duplicate_confirmation.json()["detail"] == "handoff_conflict"
    assert completed_after_confirmation.status_code == 200
    assert completed_after_confirmation.json()["status"] == "completed"
    assert cancelled_after_accept.status_code == 200
    assert cancelled_after_accept.json()["status"] == "cancelled"
    assert cross_tenant_read.status_code == 404
    assert cross_tenant_read.json()["detail"] == "handoff_not_found"
    assert cross_tenant_cancel.status_code == 404
    assert cross_tenant_cancel.json()["detail"] == "handoff_not_found"
    assert _handoff_state(client, other.id) == ("pending", None)


def test_confirmation_response_uses_committed_projection_without_requery(
    client: TestClient,
) -> None:
    from src.api.main import app
    from src.api.routes.handoff import get_service
    from src.handoff.models import HandoffDetail, HandoffStatus, HandoffUrgency
    from src.repositories import get_db_session_dependency

    tenant_id = uuid4()
    handoff_id = uuid4()
    session_id = uuid4()
    created_at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    confirmed_at = created_at + timedelta(seconds=42)
    committed = HandoffDetail(
        id=handoff_id,
        tenant_id=tenant_id,
        session_id=session_id,
        status=HandoffStatus.IN_PROGRESS,
        urgency=HandoffUrgency.NORMAL,
        reason=HandoffReason.USER_REQUEST,
        summary="canonical committed detail",
        created_at=created_at,
        updated_at=confirmed_at,
        data_hora_saida=confirmed_at,
        sla_target_seconds=600,
    )
    service = MagicMock()
    service.confirm_handoff = AsyncMock(return_value=committed)
    database = MagicMock()
    database.execute = AsyncMock(
        side_effect=AssertionError("response assembly must not requery after commit")
    )
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_db_session_dependency] = lambda: database
    try:
        response = client.post(
            f"/v1/handoffs/queue/{handoff_id}/confirm",
            headers=_jwt_headers("clinic_admin", tenant_id),
        )
    finally:
        app.dependency_overrides.pop(get_service, None)
        app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(handoff_id)
    assert response.json()["status"] == "in_progress"
    assert response.json()["tenant_id"] == str(tenant_id)
    assert response.json()["summary"] == "canonical committed detail"
    assert "wait_seconds" not in response.json()
    assert "primeira_linha_mensagem" not in response.json()
    service.confirm_handoff.assert_awaited_once_with(
        handoff_id,
        tenant_id=tenant_id,
    )
    database.execute.assert_not_awaited()


def test_checkout_response_uses_winner_projection_without_requery_or_second_call(
    client: TestClient,
) -> None:
    from src.api.main import app
    from src.api.routes.handoff import get_service
    from src.repositories import get_db_session_dependency
    from src.services.handoff_service import HandoffQueueConfirmation

    tenant_id = uuid4()
    handoff_id = uuid4()
    session_id = uuid4()
    created_at = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    confirmed_at = created_at - timedelta(seconds=5)
    committed = HandoffQueueConfirmation(
        id=handoff_id,
        numero_whatsapp="5511999999999",
        session_id=session_id,
        safety_concern=None,
        reason=HandoffReason.SAFETY_CONCERN.value,
        intencao_detectada=HandoffReason.USER_REQUEST.value,
        estagio_atual="em_qualificacao",
        primeira_linha_mensagem="private committed projection",
        created_at=created_at,
        data_hora_saida=confirmed_at,
        sla_target_seconds=600,
        status="in_progress",
    )
    service = MagicMock()
    service.confirm_queue_handoff = AsyncMock(return_value=committed)
    service.confirm_handoff = AsyncMock(
        side_effect=AssertionError("checkout must not perform a second transition")
    )
    database = MagicMock()
    database.execute = AsyncMock(
        side_effect=AssertionError("checkout must not requery after commit")
    )
    app.dependency_overrides[get_service] = lambda: service
    app.dependency_overrides[get_db_session_dependency] = lambda: database
    try:
        response = client.post(
            f"/v1/handoffs/queue/{handoff_id}/checkout",
            headers=_jwt_headers("clinic_admin", tenant_id),
        )
    finally:
        app.dependency_overrides.pop(get_service, None)
        app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "id": str(handoff_id),
        "numero_whatsapp": "5511999999999",
        "session_id": str(session_id),
        "safety_flag": True,
        "intencao_detectada": None,
        "estagio_atual": "em_qualificacao",
        "primeira_linha_mensagem": "private committed projection",
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "data_hora_saida": confirmed_at.isoformat().replace("+00:00", "Z"),
        "sla_target_seconds": 600,
        "status": "in_progress",
        "wait_seconds": -5.0,
    }
    service.confirm_queue_handoff.assert_awaited_once_with(
        handoff_id,
        tenant_id=tenant_id,
    )
    service.confirm_handoff.assert_not_awaited()
    database.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ("method", "path_template", "service_method", "body"),
    [
        ("PATCH", "/v1/handoffs/{id}", "update_handoff", {"status": "cancelled"}),
        ("POST", "/v1/handoffs/{id}/accept", "accept_handoff", None),
        (
            "POST",
            "/v1/handoffs/queue/{id}/confirm",
            "confirm_handoff",
            None,
        ),
        (
            "POST",
            "/v1/handoffs/queue/{id}/checkout",
            "confirm_queue_handoff",
            None,
        ),
        ("POST", "/v1/handoffs/{id}/complete", "complete_handoff", None),
        ("POST", "/v1/handoffs/{id}/cancel", "cancel_handoff", None),
    ],
)
@pytest.mark.parametrize(
    ("error_type", "expected_status", "expected_detail"),
    [
        (HandoffNotFoundError, 404, "handoff_not_found"),
        (HandoffConflictError, 409, "handoff_conflict"),
        (HandoffTransitionError, 409, "invalid_handoff_transition"),
        (HandoffPersistenceError, 503, "handoff_service_unavailable"),
        (HandoffPostCommitError, 503, "handoff_service_unavailable"),
    ],
)
def test_management_http_maps_typed_service_errors_without_leaking_details(
    client: TestClient,
    method: str,
    path_template: str,
    service_method: str,
    body: dict[str, str] | None,
    error_type: type[Exception],
    expected_status: int,
    expected_detail: str,
) -> None:
    from src.api.main import app
    from src.api.routes.handoff import get_service

    tenant_id = uuid4()
    service = MagicMock()
    setattr(
        service,
        service_method,
        AsyncMock(side_effect=error_type("private persistence details")),
    )
    app.dependency_overrides[get_service] = lambda: service
    try:
        response = client.request(
            method,
            path_template.format(id=uuid4()),
            headers=_jwt_headers("clinic_admin", tenant_id),
            json=body,
        )
    finally:
        app.dependency_overrides.pop(get_service, None)

    assert response.status_code == expected_status
    assert response.json()["detail"] == expected_detail
    assert "private" not in response.text


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
        "as_of",
    } <= set(queue_schema["required"])
    assert "sla_target_seconds" in item_schema["required"]
    for field in ("period_start", "period_end_exclusive", "as_of"):
        assert queue_schema["properties"][field]["format"] == "date-time"
    assert any(
        branch.get("format") == "date-time"
        for branch in schemas["HandoffDetail"]["properties"]["updated_at"]["anyOf"]
    )
    for field in ("avg_wait_seconds", "pct_within_sla"):
        assert {
            branch.get("type") for branch in queue_schema["properties"][field]["anyOf"]
        } == {"number", "null"}
    assert {
        branch.get("type")
        for branch in item_schema["properties"]["sla_target_seconds"]["anyOf"]
    } == {"integer", "null"}
    target_schema = next(
        branch
        for branch in item_schema["properties"]["sla_target_seconds"]["anyOf"]
        if branch.get("type") == "integer"
    )
    assert target_schema["minimum"] == 60
    assert target_schema["maximum"] == 7200
    assert item_schema["properties"]["status"] == {
        "$ref": "#/components/schemas/HandoffStatus"
    }
    average_schema = next(
        branch
        for branch in queue_schema["properties"]["avg_wait_seconds"]["anyOf"]
        if branch.get("type") == "number"
    )
    percentage_schema = next(
        branch
        for branch in queue_schema["properties"]["pct_within_sla"]["anyOf"]
        if branch.get("type") == "number"
    )
    assert average_schema["minimum"] == 0
    assert percentage_schema["minimum"] == 0
    assert percentage_schema["maximum"] == 100
    for field in (
        "within_sla_count",
        "confirmed_late_count",
        "unanswered_overdue_count",
        "maturing_count",
        "cancelled_unconfirmed_count",
        "unmeasured_legacy_count",
        "confirmed_count",
        "sla_eligible_count",
    ):
        assert queue_schema["properties"][field]["minimum"] == 0
    assert "lifecycle_version" not in str(queue_schema)
    assert "lifecycle_version" not in str(item_schema)

    confirm = document["paths"]["/v1/handoffs/queue/{handoff_id}/confirm"]["post"]
    checkout = document["paths"]["/v1/handoffs/queue/{handoff_id}/checkout"]["post"]
    assert confirm.get("deprecated") is not True
    assert checkout["deprecated"] is True
    assert confirm["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HandoffDetail"
    }
    assert checkout["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HandoffCheckoutResponse"
    }
    for operation in (confirm, checkout):
        assert {"404", "409", "503"} <= set(operation["responses"])
        assert operation["responses"]["404"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/HandoffNotFoundResponse"}
        assert operation["responses"]["409"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/HandoffConflictResponse"}
        assert operation["responses"]["503"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/HandoffUnavailableResponse"}
    assert schemas["HandoffNotFoundResponse"]["properties"]["detail"]["const"] == (
        "handoff_not_found"
    )
    assert set(schemas["HandoffConflictResponse"]["properties"]["detail"]["enum"]) == {
        "handoff_conflict",
        "invalid_handoff_transition",
    }
    assert (
        schemas["HandoffUnavailableResponse"]["properties"]["detail"]["const"]
        == "handoff_service_unavailable"
    )
    assert "409" in document["paths"]["/v1/handoffs/{handoff_id}"]["patch"]["responses"]
    assert (
        "409"
        in document["paths"]["/v1/handoffs/{handoff_id}/complete"]["post"]["responses"]
    )
