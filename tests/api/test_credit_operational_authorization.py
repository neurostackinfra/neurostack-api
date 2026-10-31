"""Operational authority over legal hold and purge (Group 5), SQLite.

The real application with the credit switch forced.  Every refusal is
checked to happen BEFORE any hold event, fence, storage or receipt: the
event, generation, fence, tombstone, job, result and receipt tables stay
empty and the presented authorization stays unconsumed.  The SERIALIZABLE
barriers between hold and fence run on PostgreSQL
(``tests/integration/test_credit_legal_hold_barriers.py``).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from src.api.middleware.auth import pwd_context
from src.config.settings import settings
from src.models.builder import Builder
from src.models.content_retention import (
    CreditHoldEvent,
    CreditPurgeFence,
    CreditPurgeJob,
    CreditPurgeReceipt,
    CreditPurgeResult,
    CreditRetentionTombstone,
)
from src.models.operational_capability import CreditOperationalAuthorizationReceipt
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories.database import get_db_session
from src.services import content_retention
from src.services.credit_switch import CreditSwitch

ADMIN = "/v1/admin/credits"
KEY = "credit-authorization-signing-key-for-tests-0123456789"


@dataclass
class World:
    builder: UUID
    other_builder: UUID
    tenant: UUID
    other_tenant: UUID
    api_key: str
    users: dict[str, UUID]

    def bearer(self, name: str) -> dict[str, str]:
        role = "super_admin" if name.startswith("admin") else "clinic_admin"
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(self.users[name]),
                "tenant_id": str(self.tenant),
                "role": role,
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            settings.secret_key,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}


_USERS = (
    "admin_a",
    "admin_b",
    "officer_1",
    "officer_2",
    "officer_3",
    "releaser",
    "owner",
)


async def _seed() -> World:
    world = World(
        builder=uuid4(),
        other_builder=uuid4(),
        tenant=uuid4(),
        other_tenant=uuid4(),
        api_key=secrets.token_urlsafe(24),
        users={name: uuid4() for name in _USERS},
    )
    session = get_db_session()
    try:
        async with session.begin():
            session.add(Builder(id=world.builder, name="Hold builder"))
            session.add(Builder(id=world.other_builder, name="Other builder"))
            await session.flush()
            for tenant_id, builder in (
                (world.tenant, world.builder),
                (world.other_tenant, world.other_builder),
            ):
                session.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Hold {tenant_id.hex[:6]}",
                        slug=f"hold-{tenant_id.hex}",
                        api_key_hash=pwd_context.hash(world.api_key),
                        builder_id=builder,
                        config={},
                    )
                )
            await session.flush()
            for name, user_id in world.users.items():
                session.add(
                    User(
                        id=user_id,
                        tenant_id=world.tenant,
                        email=f"{name}-{user_id.hex[:8]}@example.invalid",
                        password_hash="x",
                        role=(
                            UserRole.super_admin
                            if name.startswith("admin")
                            else UserRole.clinic_admin
                        ),
                    )
                )
    finally:
        await session.close()
    return world


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


@pytest.fixture(autouse=True)
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "credit_authorization_signing_key", SecretStr(KEY))


@pytest.fixture
def world(client: Any, switch: Any) -> World:
    return client._submit(_seed())


def _grant(client: Any, world: World, role: str, user: str, by: str = "admin_a") -> str:
    response = client.post(
        f"{ADMIN}/role-assignments",
        json={"role": role, "user_id": str(world.users[user]), "reason": "test"},
        headers=world.bearer(by),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["assignment_id"])


def _authorize(
    client: Any,
    world: World,
    purpose: str,
    subject: str,
    *,
    scope: str = "builder",
    builder: UUID | None = None,
    resource: str | None = None,
    fence_generation: int | None = None,
    by: str = "admin_b",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "purpose": purpose,
        "builder_id": str(builder or world.builder),
        "scope": scope,
        "subject_user_id": str(world.users[subject]),
    }
    if resource is not None:
        body["resource"] = resource
    if fence_generation is not None:
        body["fence_generation"] = fence_generation
    response = client.post(
        f"{ADMIN}/authorizations", json=body, headers=world.bearer(by)
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _hold(
    client: Any,
    world: World,
    action: str,
    actor: str,
    authorization: dict[str, Any],
    *,
    scope: str = "builder",
    builder: UUID | None = None,
    intent: str | None = None,
    justification: str | None = None,
) -> Any:
    body: dict[str, Any] = {
        "builder_id": str(builder or world.builder),
        "scope": scope,
        "reason_code": "litigation",
        "authorization_id": authorization["authorization_id"],
        "authorization_signature": authorization["signature"],
    }
    if intent is not None:
        body["intent_id"] = intent
    if justification is not None:
        body["justification"] = justification
    return client.post(
        f"{ADMIN}/holds/{action}", json=body, headers=world.bearer(actor)
    )


async def _count(model: Any) -> int:
    session = get_db_session()
    try:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await session.close()


def _nothing_written(client: Any, *, events: int = 0, receipts: int = 0) -> None:
    assert client._submit(_count(CreditHoldEvent)) == events
    assert client._submit(_count(CreditOperationalAuthorizationReceipt)) == receipts
    for model in (
        CreditPurgeFence,
        CreditRetentionTombstone,
        CreditPurgeJob,
        CreditPurgeResult,
        CreditPurgeReceipt,
    ):
        assert client._submit(_count(model)) == 0, model.__tablename__


def _requested(client: Any, world: World) -> str:
    _grant(client, world, "legal_hold_officer", "officer_1")
    response = _hold(
        client,
        world,
        "request",
        "officer_1",
        _authorize(client, world, "hold_request", "officer_1"),
    )
    assert response.status_code == 201, response.text
    return str(response.json()["intent_id"])


def _activated(client: Any, world: World) -> str:
    intent = _requested(client, world)
    _grant(client, world, "legal_hold_officer", "officer_2")
    response = _hold(
        client,
        world,
        "activate",
        "officer_2",
        _authorize(client, world, "hold_activate", "officer_2", resource=intent),
        intent=intent,
    )
    assert response.status_code == 201, response.text
    return intent


# ---------------------------------------------------------------------------


def test_full_hold_stream_bumps_the_generation_once_per_event(
    client: Any, world: World
) -> None:
    intent = _activated(client, world)
    _grant(client, world, "legal_hold_release_officer", "releaser")
    response = _hold(
        client,
        world,
        "release",
        "releaser",
        _authorize(client, world, "hold_release", "releaser", resource=intent),
        intent=intent,
        justification="case closed",
    )
    assert response.status_code == 201, response.text
    assert response.json()["generation"] == 3
    _nothing_written(client, events=3, receipts=3)


def test_revoked_assignment_fails_before_any_hold(client: Any, world: World) -> None:
    assignment = _grant(client, world, "legal_hold_officer", "officer_1")
    revoked = client.post(
        f"{ADMIN}/role-assignments/{assignment}/revocations",
        json={"reason": "left the company"},
        headers=world.bearer("admin_a"),
    )
    assert revoked.status_code == 201, revoked.text
    response = _hold(
        client,
        world,
        "request",
        "officer_1",
        _authorize(client, world, "hold_request", "officer_1"),
    )
    assert response.status_code == 403
    assert response.json() == {"code": "assignment_required"}
    _nothing_written(client)


def test_expired_assignment_fails_like_a_revoked_one(client: Any, world: World) -> None:
    response = client.post(
        f"{ADMIN}/role-assignments",
        json={
            "role": "legal_hold_officer",
            "user_id": str(world.users["officer_1"]),
            "reason": "temporary",
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(seconds=1)
            ).isoformat(),
        },
        headers=world.bearer("admin_a"),
    )
    assert response.status_code == 201
    authorization = _authorize(client, world, "hold_request", "officer_1")
    import time

    time.sleep(1.2)
    refused = _hold(client, world, "request", "officer_1", authorization)
    assert refused.json() == {"code": "assignment_required"}
    _nothing_written(client)


def test_reused_authorization_is_refused(client: Any, world: World) -> None:
    _grant(client, world, "legal_hold_officer", "officer_1")
    authorization = _authorize(client, world, "hold_request", "officer_1")
    first = _hold(client, world, "request", "officer_1", authorization)
    assert first.status_code == 201
    again = _hold(client, world, "request", "officer_1", authorization)
    assert again.status_code == 409
    assert again.json() == {"code": "authorization_reused"}
    _nothing_written(client, events=1, receipts=1)


def test_forged_or_altered_signature_is_refused(client: Any, world: World) -> None:
    _grant(client, world, "legal_hold_officer", "officer_1")
    authorization = _authorize(client, world, "hold_request", "officer_1")
    forged = dict(authorization, signature="0" * 64)
    response = _hold(client, world, "request", "officer_1", forged)
    assert response.json() == {"code": "authorization_invalid"}
    _nothing_written(client)


def test_cross_env_authorization_is_refused(
    client: Any, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grant(client, world, "legal_hold_officer", "officer_1")
    home = settings.environment
    foreign = "staging" if home != "staging" else "development"
    monkeypatch.setattr(settings, "environment", foreign)
    authorization = _authorize(client, world, "hold_request", "officer_1")
    assert authorization["payload"]["environment"] == foreign
    monkeypatch.setattr(settings, "environment", home)
    response = _hold(client, world, "request", "officer_1", authorization)
    assert response.status_code == 403
    assert response.json() == {"code": "authorization_cross_env"}
    _nothing_written(client)


def test_cross_scope_authorization_is_refused(client: Any, world: World) -> None:
    _grant(client, world, "legal_hold_officer", "officer_1")
    tenant_scope = f"tenant:{world.tenant}"
    narrow = _authorize(client, world, "hold_request", "officer_1", scope=tenant_scope)
    response = _hold(client, world, "request", "officer_1", narrow, scope="builder")
    assert response.json() == {"code": "authorization_cross_scope"}
    other = _authorize(
        client, world, "hold_request", "officer_1", builder=world.other_builder
    )
    response = _hold(client, world, "request", "officer_1", other)
    assert response.json() == {"code": "authorization_cross_scope"}
    # A tenant of another builder is never a scope of this builder.
    foreign = _authorize(
        client, world, "hold_request", "officer_1", scope=f"tenant:{world.other_tenant}"
    )
    response = _hold(
        client,
        world,
        "request",
        "officer_1",
        foreign,
        scope=f"tenant:{world.other_tenant}",
    )
    assert response.status_code == 404
    assert response.json() == {"code": "scope_not_found"}
    _nothing_written(client)


def test_cross_scope_probe_without_the_role_learns_nothing(
    client: Any, world: World
) -> None:
    """A caller without the officer role is refused ``assignment_required``
    (403) before any existence check: a foreign tenant, an unknown builder
    and a real scope all answer alike, so the role-less caller cannot probe
    which builders or clinics exist."""

    answers = []
    for builder, scope in (
        (world.builder, "builder"),
        (world.builder, f"tenant:{world.other_tenant}"),
        (uuid4(), "builder"),
    ):
        authorization = _authorize(
            client, world, "hold_request", "officer_2", scope=scope, builder=builder
        )
        response = _hold(
            client,
            world,
            "request",
            "officer_2",
            authorization,
            scope=scope,
            builder=builder,
        )
        answers.append((response.status_code, response.json()))
    assert answers == [(403, {"code": "assignment_required"})] * 3
    _nothing_written(client)


def test_self_release_is_refused(client: Any, world: World) -> None:
    intent = _activated(client, world)
    # The requester also holds the release role: still refused.
    _grant(client, world, "legal_hold_release_officer", "officer_1")
    response = _hold(
        client,
        world,
        "release",
        "officer_1",
        _authorize(client, world, "hold_release", "officer_1", resource=intent),
        intent=intent,
        justification="mine",
    )
    assert response.status_code == 403
    assert response.json() == {"code": "self_release_forbidden"}
    _nothing_written(client, events=2, receipts=2)


def test_separation_of_duties_needs_the_release_role(client: Any, world: World) -> None:
    intent = _activated(client, world)
    _grant(client, world, "legal_hold_officer", "officer_3")
    response = _hold(
        client,
        world,
        "release",
        "officer_3",
        _authorize(client, world, "hold_release", "officer_3", resource=intent),
        intent=intent,
        justification="not my role",
    )
    assert response.json() == {"code": "assignment_required"}
    _nothing_written(client, events=2, receipts=2)


def test_separation_of_duties_super_admin_alone_is_no_officer(
    client: Any, world: World
) -> None:
    response = _hold(
        client,
        world,
        "request",
        "admin_a",
        _authorize(client, world, "hold_request", "admin_a"),
    )
    assert response.json() == {"code": "assignment_required"}
    _nothing_written(client)


def test_purge_disabled_for_every_call_whatever_is_presented(
    client: Any, world: World
) -> None:
    _grant(client, world, "production_data_lifecycle_owner", "admin_b")
    authorization = _authorize(
        client,
        world,
        "purge",
        "admin_b",
        resource=str(uuid4()),
        fence_generation=0,
        by="admin_a",
    )
    for body in (
        {},
        {"enabled": True, "purge_enabled": True, "force": True},
        {"environment": "production", "expected_environment": "test"},
        {
            "authorization_id": authorization["authorization_id"],
            "authorization_signature": authorization["signature"],
            "receipt": {"status": "approved", "digest": "f" * 64},
        },
        {
            "builder_id": str(world.builder),
            "scope": "builder",
            "operation_id": str(uuid4()),
        },
    ):
        response = client.post(
            f"{ADMIN}/purges", json=body, headers=world.bearer("admin_b")
        )
        assert response.status_code == 409
        assert response.json() == {"code": "purge_disabled"}
    malformed = client.post(
        f"{ADMIN}/purges", content=b"{not json", headers=world.bearer("admin_b")
    )
    assert malformed.json() == {"code": "purge_disabled"}
    _nothing_written(client)


def test_purge_disabled_refuses_non_super_admin_first(
    client: Any, world: World
) -> None:
    response = client.post(
        f"{ADMIN}/purges", json={}, headers=world.bearer("officer_1")
    )
    assert response.status_code == 403
    api_key = client.post(
        f"{ADMIN}/purges", json={}, headers={"X-API-Key": world.api_key}
    )
    assert api_key.status_code == 403
    _nothing_written(client)


async def test_purge_disabled_service_entry_ignores_everything() -> None:
    outcome = await content_retention.request_purge(
        enabled=True, environment="production", receipt="forged", flag=1
    )
    assert outcome.code == "purge_disabled"


def test_switch_off_answers_credit_unavailable(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    switch(False)
    for path in ("/role-assignments", "/authorizations", "/holds/request", "/purges"):
        response = client.post(
            f"{ADMIN}{path}", json={}, headers=world.bearer("admin_a")
        )
        assert response.status_code == 503
        assert response.json() == {"code": "credit_unavailable"}
