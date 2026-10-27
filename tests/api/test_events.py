"""
NeuroStack API - Events Endpoint Tests
"""

from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def scoped_session_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    """These response-shape tests operate on an existing tenant session."""

    from src.models.session import Session
    from src.repositories.session import SessionRepository

    async def get_by_tenant_for_update(
        _repo: SessionRepository, session_id: UUID, tenant_id: UUID
    ) -> Session:
        return Session(
            id=session_id,
            tenant_id=tenant_id,
            channel="test",
            cognitive_state={},
            session_metadata={},
            message_count=0,
        )

    monkeypatch.setattr(
        SessionRepository, "get_by_tenant_for_update", get_by_tenant_for_update
    )


class TestEventsEndpoint:
    """Test suite for events endpoint."""

    def test_process_event_returns_200(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns 200 OK."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "event_id" in data
        assert "decision" in data
        assert "state_patch" in data
        assert "prompt" in data
        assert "context" in data
        assert data["response_language"] == "pt-BR"

    def test_process_event_returns_decision(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns a valid decision."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        decision = data["decision"]

        assert "intent" in decision
        assert "funnel_stage" in decision
        assert "action" in decision
        assert "confidence" in decision
        assert isinstance(decision["confidence"], float)
        assert 0 <= decision["confidence"] <= 1
        assert "safety_concern" in decision
        assert "needs_rag" in decision
        assert "handoff_required" in decision

    def test_process_event_returns_prompt(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns a valid prompt."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        prompt = data["prompt"]

        assert "system" in prompt
        assert "instructions" in prompt
        assert "constraints" in prompt
        assert isinstance(prompt["constraints"], list)

    def test_process_event_returns_context(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that event processing returns context."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        context = data["context"]

        assert "recent_messages" in context
        assert isinstance(context["recent_messages"], list)

    def test_process_event_with_invalid_tenant_id(
        self,
        client: TestClient,
        api_headers: dict[str, str],
    ) -> None:
        """Test that invalid tenant ID returns validation error."""
        payload = {
            "tenant_id": "invalid-uuid",
            "session_id": "c0000000-0000-0000-0000-000000000001",
            "event_type": "message.in",
            "payload": {
                "content": "Hello",
                "channel": "whatsapp",
            },
        }

        response = client.post("/v1/events", json=payload, headers=api_headers)
        assert response.status_code == 422

    def test_process_event_with_empty_content(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that empty content returns validation error."""
        sample_event_payload["payload"]["content"] = ""

        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )
        assert response.status_code == 422

    def test_process_event_mode_is_normal(
        self,
        client: TestClient,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test that default mode is normal."""
        response = client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        data = response.json()
        assert data["mode"] == "normal"


class TestEventsEndpointAsync:
    """Async test suite for events endpoint."""

    @pytest.mark.asyncio
    async def test_process_event_async(
        self,
        async_client,
        sample_event_payload: dict[str, Any],
        api_headers: dict[str, str],
    ) -> None:
        """Test event processing with async client."""
        response = await async_client.post(
            "/v1/events", json=sample_event_payload, headers=api_headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "decision" in data


def _seed_rows(client: TestClient, *rows: Any) -> None:
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            for row in rows:
                db.add(row)
                await db.flush()
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "event_type", ["ns.atendimento_tipo_set", "flow.transition", "mode.change"]
)
def test_store_event_types_list_and_detail_without_500_or_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    """The three Event Store types once missing from the public EventType."""

    from uuid import uuid4

    from src.api.middleware.auth import APIKeyStore, TenantContext
    from src.models.event import Event
    from src.models.session import Session
    from src.models.tenant import Tenant

    tenant_id, session_id = uuid4(), uuid4()
    event = Event(
        id=uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=event_type,
        interaction_origin="production",
        event_data={"from_mode": "normal", "to_mode": "handoff"},
        event_metadata={},
    )
    _seed_rows(
        client,
        Tenant(
            id=tenant_id,
            name="Store types clinic",
            slug=f"store-types-{tenant_id}",
            api_key_hash=f"store-types-{tenant_id}",
            config={},
        ),
        Session(
            id=session_id,
            tenant_id=tenant_id,
            channel="whatsapp",
            cognitive_state={},
            session_metadata={},
        ),
        event,
    )

    async def authenticate(_cls: type[APIKeyStore], api_key: str) -> TenantContext:
        return TenantContext(tenant_id=tenant_id, api_key=api_key, is_active=True)

    monkeypatch.setattr(APIKeyStore, "validate_api_key", classmethod(authenticate))
    headers = {"X-API-Key": "store-types-key"}

    by_session = client.get(
        "/v1/events",
        params={"tenant_id": str(tenant_id), "session_id": str(session_id)},
        headers=headers,
    )
    by_tenant = client.get(
        "/v1/events", params={"tenant_id": str(tenant_id)}, headers=headers
    )
    by_type = client.get(
        "/v1/events",
        params={"tenant_id": str(tenant_id), "event_type": event_type},
        headers=headers,
    )
    detail = client.get(f"/v1/events/{event.id}", headers=headers)

    for listed in (by_session, by_tenant, by_type):
        assert listed.status_code == 200, listed.text
        assert [item["event_type"] for item in listed.json()] == [event_type]
        assert listed.json()[0]["interaction_origin"] == "production"
    assert detail.status_code == 200, detail.text
    assert detail.json()["event_id"] == str(event.id)
    assert detail.json()["event_type"] == event_type
    assert detail.json()["interaction_origin"] == "production"


def test_event_type_covers_every_event_store_type() -> None:
    from src.api.schemas.events import EventType
    from src.memory.event_store import EventType as StoredEventType

    assert {item.value for item in StoredEventType} <= {
        item.value for item in EventType
    }


def test_process_event_exposes_the_interaction_id_field(
    client: TestClient,
    sample_event_payload: dict[str, Any],
    api_headers: dict[str, str],
) -> None:
    response = client.post("/v1/events", json=sample_event_payload, headers=api_headers)

    assert response.status_code == 200
    data = response.json()
    assert "interaction_event_id" in data
    assert data["interaction_event_id"] != data["event_id"]
