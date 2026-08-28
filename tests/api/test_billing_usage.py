"""API tests for builder plan usage counters."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt

from src.config.settings import settings
from src.models.builder import Builder
from src.models.event import Event
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories import get_db_session


def _token(role: str, *, builder_id: UUID | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(uuid4()),
        "role": role,
        "email": f"{role}@test.com",
        "iat": now,
        "exp": now + timedelta(hours=1),
        "jti": str(uuid4()),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


async def _seed_usage(builder_id: UUID) -> tuple[int, int]:
    session = get_db_session()
    try:
        builder = Builder(id=builder_id, name="Usage API Builder")
        tenant = Tenant(
            id=uuid4(),
            name="Usage Clinic",
            slug=f"usage-{uuid4().hex}",
            api_key_hash=f"hash-{uuid4().hex}",
            builder_id=builder_id,
            config={},
        )
        other_builder = Builder(id=uuid4(), name="Other Builder")
        other_tenant = Tenant(
            id=uuid4(),
            name="Other Clinic",
            slug=f"other-{uuid4().hex}",
            api_key_hash=f"hash-{uuid4().hex}",
            builder_id=other_builder.id,
            config={},
        )
        session.add_all([builder, tenant, other_builder, other_tenant])

        now = datetime.now(timezone.utc)
        session.add_all(
            [
                Event(
                    tenant_id=tenant.id,
                    event_type="message.in",
                    event_data={},
                    event_metadata={},
                    created_at=now,
                ),
                Event(
                    tenant_id=tenant.id,
                    event_type="srm.decision",
                    event_data={},
                    event_metadata={},
                    created_at=now,
                ),
                Event(
                    tenant_id=other_tenant.id,
                    event_type="message.in",
                    event_data={},
                    event_metadata={},
                    created_at=now,
                ),
            ]
        )
        content = "Conteúdo RAG"
        session.add(
            KnowledgeDocument(
                tenant_id=tenant.id,
                title="Usage document",
                content=content,
                document_metadata={},
            )
        )
        await session.commit()
        return 1, len(content.encode("utf-8"))
    finally:
        await session.close()


def test_usage_requires_builder_auth(client) -> None:
    assert client.get("/v1/billing/usage").status_code == 401
    response = client.get(
        "/v1/billing/usage",
        headers={"Authorization": f"Bearer {_token('clinic_admin')}"},
    )
    assert response.status_code == 403


def test_usage_and_summary_are_scoped_to_authenticated_builder(client) -> None:
    builder_id = uuid4()
    expected_traces, expected_rag_bytes = client._submit(_seed_usage(builder_id))
    headers = {"Authorization": f"Bearer {_token('builder', builder_id=builder_id)}"}

    response = client.get("/v1/billing/usage", headers=headers)
    assert response.status_code == 200
    usage = response.json()

    assert usage["traces"]["used"] == expected_traces
    assert usage["traces"]["included"] == settings.start_plan_monthly_trace_limit
    assert usage["traces"]["available"] == (
        settings.start_plan_monthly_trace_limit - expected_traces
    )
    assert usage["storage"]["rag"]["used_bytes"] == expected_rag_bytes
    assert usage["storage"]["media"]["used_bytes"] == 0
    assert usage["credits"] == {
        "purchase_enabled": False,
        "coming_soon": True,
    }

    summary_response = client.get("/v1/billing/summary", headers=headers)
    assert summary_response.status_code == 200
    assert summary_response.json()["usage"]["traces"]["used"] == expected_traces
