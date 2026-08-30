"""Contract tests for session management and the permanent-pause queue."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.settings import settings


def _jwt_headers(
    role: str,
    tenant_id: UUID,
    *,
    builder_id: UUID | None = None,
) -> dict[str, str]:
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _session(
    tenant_id: UUID,
    *,
    session_id: UUID | None = None,
    user_id: str | None,
    activity: datetime,
    paused: bool = True,
    paused_until: datetime | None = None,
    active: bool = True,
    channel: str = "whatsapp",
    intent: object = "scheduling",
    funnel: object = "em_qualificacao",
):
    from src.models.session import Session

    return Session(
        id=session_id or uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        channel=channel,
        cognitive_state={
            "last_intent": intent,
            "funnel_stage": funnel,
            "private": {"must_not_leak": True},
        },
        session_metadata={},
        last_activity_at=activity,
        is_active=active,
        is_paused=paused,
        paused_until=paused_until,
    )


def _seed(
    client: TestClient,
    *,
    tenants: list[tuple[UUID, UUID | None]],
    sessions: list[Any] | None = None,
    builder_ids: list[UUID] | None = None,
    handoffs: list[Any] | None = None,
) -> None:
    from src.models.builder import Builder
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            for builder_id in builder_ids or []:
                db.add(Builder(id=builder_id, name=f"Builder {builder_id}"))
            for tenant_id, builder_id in tenants:
                db.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Clinic {tenant_id}",
                        slug=f"clinic-{tenant_id}",
                        api_key_hash=f"hash-{tenant_id}",
                        builder_id=builder_id,
                        config={},
                    )
                )
            db.add_all(sessions or [])
            db.add_all(handoffs or [])
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _session_pause_state(client: TestClient, session_id: UUID) -> tuple[bool, Any]:
    from src.models.session import Session
    from src.repositories.database import get_db_session

    async def _run() -> tuple[bool, Any]:
        db = get_db_session()
        try:
            result = await db.execute(
                select(Session.is_paused, Session.paused_until).where(
                    Session.id == session_id
                )
            )
            row = result.one()
            return bool(row.is_paused), row.paused_until
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


def _handoff_snapshot(client: TestClient, handoff_id: UUID) -> dict[str, Any]:
    from src.models.handoff import HandoffRecord
    from src.repositories.database import get_db_session

    async def _run() -> dict[str, Any]:
        db = get_db_session()
        try:
            record = await db.get(HandoffRecord, handoff_id)
            assert record is not None
            return record.to_dict()
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


class TestInactiveSessionsQueue:
    def test_contract_effective_filter_order_and_pagination(
        self, client: TestClient
    ) -> None:
        tenant_id = uuid4()
        now = datetime.now(timezone.utc)
        tie_time = now - timedelta(minutes=2)
        low_id = UUID("aaaaaaaa-0000-0000-0000-000000000000")
        high_id = UUID("ffffffff-0000-0000-0000-000000000000")
        newest_unique = _session(
            tenant_id, user_id="newest", activity=now, channel="web"
        )
        tie_low = _session(
            tenant_id,
            session_id=low_id,
            user_id="same-timestamp",
            activity=tie_time,
        )
        tie_high = _session(
            tenant_id,
            session_id=high_id,
            user_id="same-timestamp",
            activity=tie_time,
        )
        null_identity = _session(
            tenant_id,
            user_id=None,
            activity=now - timedelta(minutes=3),
            intent=42,
            funnel=None,
        )
        second_null_identity = _session(
            tenant_id,
            user_id=None,
            activity=now - timedelta(minutes=4),
        )
        hidden_old_pause = _session(
            tenant_id,
            user_id="effective-unpaused",
            activity=now - timedelta(minutes=5),
        )
        effective_unpaused = _session(
            tenant_id,
            user_id="effective-unpaused",
            activity=now - timedelta(minutes=4),
            paused=False,
        )
        temporary = _session(
            tenant_id,
            user_id="temporary",
            activity=now + timedelta(minutes=1),
            paused_until=now + timedelta(hours=1),
        )
        ended = _session(
            tenant_id,
            user_id="ended",
            activity=now + timedelta(minutes=2),
            active=False,
        )
        _seed(
            client,
            tenants=[(tenant_id, None)],
            sessions=[
                newest_unique,
                tie_low,
                tie_high,
                null_identity,
                second_null_identity,
                hidden_old_pause,
                effective_unpaused,
                temporary,
                ended,
            ],
        )
        headers = _jwt_headers("clinic_admin", tenant_id)

        first = client.get(
            "/v1/sessions/inactive",
            params={"page": 1, "page_size": 2},
            headers=headers,
        )

        assert first.status_code == 200, first.text
        assert first.json()["total"] == 4
        assert first.json()["page"] == 1
        assert first.json()["page_size"] == 2
        assert first.json()["has_next"] is True
        assert first.json()["has_previous"] is False
        assert [item["session_id"] for item in first.json()["items"]] == [
            str(newest_unique.id),
            str(high_id),
        ]
        assert set(first.json()["items"][0]) == {
            "session_id",
            "contact_identifier",
            "channel",
            "last_intent",
            "funnel_stage",
            "last_activity_at",
        }
        assert "cognitive_state" not in first.text
        assert "must_not_leak" not in first.text

        second = client.get(
            "/v1/sessions/inactive",
            params={"page": 2, "page_size": 2},
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["has_next"] is False
        assert second.json()["has_previous"] is True
        assert [item["session_id"] for item in second.json()["items"]] == [
            str(null_identity.id),
            str(second_null_identity.id),
        ]
        assert second.json()["items"][0]["contact_identifier"] is None
        assert second.json()["items"][0]["last_intent"] is None
        assert second.json()["items"][0]["funnel_stage"] == "lead_novo"

    @pytest.mark.parametrize("role", ["investor", "support_agent"])
    def test_non_operational_roles_are_forbidden(
        self, client: TestClient, role: str
    ) -> None:
        tenant_id = uuid4()
        session = _session(
            tenant_id, user_id="forbidden", activity=datetime.now(timezone.utc)
        )
        _seed(client, tenants=[(tenant_id, None)], sessions=[session])
        headers = _jwt_headers(role, tenant_id)

        listed = client.get("/v1/sessions/inactive", headers=headers)
        reactivated = client.post(
            f"/v1/sessions/{session.id}/reactivate", headers=headers
        )

        assert listed.status_code == 403
        assert reactivated.status_code == 403
        assert _session_pause_state(client, session.id) == (True, None)

    def test_api_key_without_operational_role_is_forbidden(
        self, client: TestClient, api_headers: dict[str, str]
    ) -> None:
        assert (
            client.get("/v1/sessions/inactive", headers=api_headers).status_code == 403
        )


class TestInactiveSessionTenantAuthorization:
    def test_clinic_admin_uses_own_tenant_and_cannot_override(
        self, client: TestClient
    ) -> None:
        own_tenant = uuid4()
        other_tenant = uuid4()
        own = _session(own_tenant, user_id="own", activity=datetime.now(timezone.utc))
        other = _session(
            other_tenant, user_id="other", activity=datetime.now(timezone.utc)
        )
        _seed(
            client,
            tenants=[(own_tenant, None), (other_tenant, None)],
            sessions=[own, other],
        )
        headers = _jwt_headers("clinic_admin", own_tenant)

        own_response = client.get("/v1/sessions/inactive", headers=headers)
        override_response = client.get(
            "/v1/sessions/inactive",
            params={"tenant_id": str(other_tenant)},
            headers=headers,
        )
        cross_reactivate = client.post(
            f"/v1/sessions/{other.id}/reactivate", headers=headers
        )

        assert [item["session_id"] for item in own_response.json()["items"]] == [
            str(own.id)
        ]
        assert override_response.status_code == 400
        assert cross_reactivate.status_code == 404
        assert _session_pause_state(client, other.id) == (True, None)

    def test_linked_builder_override_and_unlinked_rejection(
        self, client: TestClient
    ) -> None:
        builder_id = uuid4()
        builder_tenant = uuid4()
        linked_tenant = uuid4()
        unlinked_tenant = uuid4()
        linked = _session(
            linked_tenant, user_id="linked", activity=datetime.now(timezone.utc)
        )
        _seed(
            client,
            tenants=[(linked_tenant, builder_id), (unlinked_tenant, None)],
            sessions=[linked],
            builder_ids=[builder_id],
        )
        headers = _jwt_headers("builder", builder_tenant, builder_id=builder_id)

        allowed = client.get(
            "/v1/sessions/inactive",
            params={"tenant_id": str(linked_tenant)},
            headers=headers,
        )
        denied_list = client.get(
            "/v1/sessions/inactive",
            params={"tenant_id": str(unlinked_tenant)},
            headers=headers,
        )
        denied_command = client.post(
            f"/v1/sessions/{linked.id}/reactivate",
            params={"tenant_id": str(unlinked_tenant)},
            headers=headers,
        )

        assert allowed.status_code == 200
        assert allowed.json()["items"][0]["session_id"] == str(linked.id)
        assert denied_list.status_code == 403
        assert denied_command.status_code == 403

    def test_super_admin_explicit_override(self, client: TestClient) -> None:
        admin_tenant = uuid4()
        clinic_tenant = uuid4()
        session = _session(
            clinic_tenant, user_id="clinic", activity=datetime.now(timezone.utc)
        )
        _seed(client, tenants=[(clinic_tenant, None)], sessions=[session])
        headers = _jwt_headers("super_admin", admin_tenant)

        response = client.get(
            "/v1/sessions/inactive",
            params={"tenant_id": str(clinic_tenant)},
            headers=headers,
        )

        assert response.status_code == 200
        assert response.json()["items"][0]["session_id"] == str(session.id)


class TestSessionReactivation:
    @pytest.mark.asyncio
    async def test_update_miss_retries_effective_permanent_pause(self) -> None:
        from src.repositories.session import SessionRepository

        tenant_id = uuid4()
        session_id = uuid4()
        db = AsyncMock()
        permanently_paused = SimpleNamespace(
            id=session_id,
            user_id="retry-user",
            is_active=True,
            is_paused=True,
            paused_until=None,
            effective_id=session_id,
        )
        db.execute.side_effect = [
            SimpleNamespace(rowcount=0),
            SimpleNamespace(one_or_none=lambda: permanently_paused),
            SimpleNamespace(rowcount=1),
        ]

        result = await SessionRepository(db).reactivate_effective(tenant_id, session_id)

        assert result == "reactivated"
        assert db.execute.await_count == 3
        db.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_persistent_update_miss_never_reports_already_active(self) -> None:
        from src.repositories.session import SessionRepository

        tenant_id = uuid4()
        session_id = uuid4()
        db = AsyncMock()
        permanently_paused = SimpleNamespace(
            id=session_id,
            user_id="churning-user",
            is_active=True,
            is_paused=True,
            paused_until=None,
            effective_id=session_id,
        )
        db.execute.side_effect = [
            item
            for _ in range(SessionRepository._REACTIVATION_MAX_ATTEMPTS)
            for item in (
                SimpleNamespace(rowcount=0),
                SimpleNamespace(one_or_none=lambda: permanently_paused),
            )
        ]

        result = await SessionRepository(db).reactivate_effective(tenant_id, session_id)

        assert result == "reactivation_conflict"
        db.flush.assert_not_awaited()

    def test_reactivation_is_idempotent_and_preserves_handoff(
        self, client: TestClient
    ) -> None:
        from src.models.handoff import HandoffRecord

        tenant_id = uuid4()
        session = _session(
            tenant_id, user_id="handoff-user", activity=datetime.now(timezone.utc)
        )
        accepted_handoff = HandoffRecord(
            tenant_id=tenant_id,
            session_id=session.id,
            reason="human_requested",
            summary="unchanged",
            context={"nested": {"value": 1}},
            status="accepted",
            urgency="high",
            assigned_to="operator-1",
            webhook_sent=True,
            webhook_response={"ok": True},
            numero_whatsapp="5511999999999",
        )
        pending_handoff = HandoffRecord(
            tenant_id=tenant_id,
            session_id=session.id,
            reason="operator_takeover",
            summary="also unchanged",
            context={"nested": {"value": 2}},
            status="pending",
            urgency="normal",
            numero_whatsapp="5511888888888",
        )
        _seed(
            client,
            tenants=[(tenant_id, None)],
            sessions=[session],
            handoffs=[accepted_handoff, pending_handoff],
        )
        headers = _jwt_headers("clinic_admin", tenant_id)
        accepted_before = _handoff_snapshot(client, accepted_handoff.id)
        pending_before = _handoff_snapshot(client, pending_handoff.id)

        first = client.post(f"/v1/sessions/{session.id}/reactivate", headers=headers)
        second = client.post(f"/v1/sessions/{session.id}/reactivate", headers=headers)
        handoff_queue = client.get(
            "/v1/handoffs/queue",
            params={"tenant_id": str(tenant_id)},
            headers=headers,
        )

        assert first.status_code == 200, first.text
        assert first.json() == {
            "session_id": str(session.id),
            "status": "reactivated",
        }
        assert second.status_code == 200
        assert second.json()["status"] == "already_active"
        assert _session_pause_state(client, session.id) == (False, None)
        assert _handoff_snapshot(client, accepted_handoff.id) == accepted_before
        assert _handoff_snapshot(client, pending_handoff.id) == pending_before
        assert handoff_queue.status_code == 200
        queue_ids = {item["id"] for item in handoff_queue.json()["items"]}
        assert queue_ids >= {str(accepted_handoff.id), str(pending_handoff.id)}

    def test_superseded_active_session_returns_conflict_without_mutation(
        self, client: TestClient
    ) -> None:
        tenant_id = uuid4()
        now = datetime.now(timezone.utc)
        old = _session(
            tenant_id, user_id="duplicate", activity=now - timedelta(minutes=1)
        )
        effective = _session(tenant_id, user_id="duplicate", activity=now)
        _seed(
            client,
            tenants=[(tenant_id, None)],
            sessions=[old, effective],
        )
        headers = _jwt_headers("clinic_admin", tenant_id)

        response = client.post(f"/v1/sessions/{old.id}/reactivate", headers=headers)

        assert response.status_code == 409
        assert response.json()["detail"] == "superseded_session"
        assert _session_pause_state(client, old.id) == (True, None)
        assert _session_pause_state(client, effective.id) == (True, None)

    def test_temporary_ended_and_missing_rejections(self, client: TestClient) -> None:
        tenant_id = uuid4()
        now = datetime.now(timezone.utc)
        temporary = _session(
            tenant_id,
            user_id="temporary",
            activity=now,
            paused_until=now + timedelta(hours=1),
        )
        ended = _session(
            tenant_id,
            user_id="ended",
            activity=now,
            active=False,
        )
        _seed(
            client,
            tenants=[(tenant_id, None)],
            sessions=[temporary, ended],
        )
        headers = _jwt_headers("clinic_admin", tenant_id)

        temporary_response = client.post(
            f"/v1/sessions/{temporary.id}/reactivate", headers=headers
        )
        ended_response = client.post(
            f"/v1/sessions/{ended.id}/reactivate", headers=headers
        )
        missing_response = client.post(
            f"/v1/sessions/{uuid4()}/reactivate", headers=headers
        )

        assert temporary_response.status_code == 409
        assert temporary_response.json()["detail"] == "temporary_pause"
        assert ended_response.status_code == 404
        assert missing_response.status_code == 404
        paused, paused_until = _session_pause_state(client, temporary.id)
        assert paused is True
        assert paused_until is not None


class TestExistingSessionsContract:
    def test_create_and_list_session(self, client: TestClient) -> None:
        tenant_id = uuid4()
        _seed(client, tenants=[(tenant_id, None)])
        headers = _jwt_headers("clinic_admin", tenant_id)

        created = client.post(
            "/v1/sessions",
            json={"tenant_id": str(tenant_id)},
            headers=headers,
        )
        listed = client.get(
            "/v1/sessions",
            params={
                "tenant_id": str(tenant_id),
                "page": 1,
                "page_size": 10,
            },
            headers=headers,
        )

        assert created.status_code == 201
        assert created.json()["channel"] == "unknown"
        assert created.json()["user_id"] is None
        assert created.json()["cognitive_state"]["funnel_stage"] == "lead_novo"
        assert listed.status_code == 200
        assert listed.json()["total"] == 1
        assert listed.json()["page"] == 1
        assert listed.json()["page_size"] == 10
