"""Contract tests for handoff queue projection and checkout."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
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
) -> Any:
    from src.models.handoff import HandoffRecord

    return HandoffRecord(
        id=uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        reason=reason,
        summary="queue contract",
        context=context,
        status="pending",
        urgency="normal",
        numero_whatsapp="5511999999999",
        intencao_detectada=intent,
        estagio_atual="em_qualificacao",
        primeira_linha_mensagem=first_message,
        created_at=created_at,
    )


def _seed(
    client: TestClient,
    *,
    tenants: list[UUID],
    sessions: list[Any],
    handoffs: list[Any],
) -> None:
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
    for item in items:
        safety_flag, intent, _ = expected[item["id"]]
        assert item["safety_flag"] is safety_flag
        assert item["intencao_detectada"] == intent
        assert item["primeira_linha_mensagem"] is None

        checked_out = client.post(
            f"/v1/handoffs/queue/{item['id']}/checkout",
            headers=headers,
        )
        assert checked_out.status_code == 200, checked_out.text
        checkout_body = checked_out.json()
        assert checkout_body["safety_flag"] is safety_flag
        assert checkout_body["intencao_detectada"] == intent
        assert checkout_body["primeira_linha_mensagem"] == expected[item["id"]][2]
        assert checkout_body["status"] == "completed"
        assert checkout_body["data_hora_saida"] is not None
        assert checkout_body["wait_seconds"] is not None

    duplicate = client.post(
        f"/v1/handoffs/queue/{records[0].id}/checkout",
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "already_completed"


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
    assert tenant_override.json()["detail"] == "tenant_mismatch"
    assert cross_tenant_checkout.status_code == 403
    assert cross_tenant_checkout.json()["detail"] == "tenant_mismatch"
    assert _handoff_state(client, other.id) == ("pending", None)
