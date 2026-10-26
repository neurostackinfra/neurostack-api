"""Principal derivation on the real app (neurostack-credit-system Group 1b).

Through ``src.api.main.app`` (real middleware stack and lifespan, SQLite):

* an accepted ``POST /v1/events`` (``X-API-Key`` checked against
  ``tenants.api_key_hash``, or a JWT) and an accepted Meta delivery (HMAC)
  expose on ``request.state`` exactly the identity ``main`` authenticated and
  the switch value captured at entry; the derived principal uses the builder
  re-read from ``tenants`` -- never the JWT ``builder_id`` claim;
* a rejected request -- invalid or missing credentials, an expired token, a
  cross-tenant assertion or session, a bad Meta signature, an inactive Meta
  tenant, a non-canonical/unknown Z-API token or instance, an unknown
  Uazapi/Evolution instance -- answers exactly like ``main`` with the switch
  off and forced on, publishes no identity, never reaches the replay claim,
  writes no row in any ``credit_*`` table and issues no SQL that touches one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import Request
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import pwd_context
from src.api.routes import webhook
from src.config.settings import settings
from src.models.base import Base
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.database import (
    get_db_engine,
    get_db_session,
    get_db_session_dependency,
)
from src.services.credit_principal import (
    CREDIT_IDENTITY_STATE_KEY,
    AuthenticatedIdentity,
    derive_credit_principal,
    read_tenant_builder,
)
from src.services.credit_switch import CREDIT_SWITCH_STATE_KEY, CreditSwitch

EVENTS = "/v1/events"
META = "/v1/webhook/meta"
_CREDIT_TABLES = sorted(t for t in Base.metadata.tables if t.startswith("credit_"))


# ---------------------------------------------------------------------------
# A seeded world: one tenant per ingress (fresh SQLite per lifespan).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    builder: UUID
    api_tenant: UUID
    api_key: str
    api_session: UUID
    jwt_tenant: UUID  # standalone
    jwt_session: UUID
    meta_tenant: UUID
    meta_phone: str
    meta_secret: str
    inactive_meta_tenant: UUID
    inactive_meta_phone: str
    inactive_meta_secret: str
    zapi_tenant: UUID
    zapi_token: str
    zapi_instance: str
    uazapi_tenant: UUID
    evolution_tenant: UUID


def _capability_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _tenant(tenant_id: UUID, *, builder: UUID | None, **extra: Any) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=f"Credit principal {tenant_id.hex[:8]}",
        slug=f"credit-principal-{tenant_id.hex}",
        api_key_hash=extra.pop("api_key_hash", f"unused-{tenant_id.hex}"),
        builder_id=builder,
        config={},
        **extra,
    )


def _session(session_id: UUID, tenant_id: UUID) -> Session:
    return Session(
        id=session_id,
        tenant_id=tenant_id,
        channel="test",
        cognitive_state={},
        session_metadata={},
    )


async def _seed(world: World) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            database.add(Builder(id=world.builder, name="Credit principal builder"))
            await database.flush()
            database.add_all(
                [
                    _tenant(
                        world.api_tenant,
                        builder=world.builder,
                        api_key_hash=pwd_context.hash(world.api_key),
                    ),
                    _tenant(world.jwt_tenant, builder=None),
                    _tenant(world.meta_tenant, builder=world.builder),
                    _tenant(world.inactive_meta_tenant, builder=None, is_active=False),
                    _tenant(world.zapi_tenant, builder=world.builder),
                    _tenant(world.uazapi_tenant, builder=None),
                    _tenant(world.evolution_tenant, builder=world.builder),
                ]
            )
            await database.flush()
            database.add_all(
                [
                    _session(world.api_session, world.api_tenant),
                    _session(world.jwt_session, world.jwt_tenant),
                    ClinicConfigTable(
                        tenant_id=world.meta_tenant,
                        api_credentials={
                            "whatsapp_provider": "meta",
                            "meta_phone_number_id": world.meta_phone,
                            "meta_app_secret": world.meta_secret,
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=world.inactive_meta_tenant,
                        api_credentials={
                            "whatsapp_provider": "meta",
                            "meta_phone_number_id": world.inactive_meta_phone,
                            "meta_app_secret": world.inactive_meta_secret,
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=world.zapi_tenant,
                        api_credentials={
                            "whatsapp_provider": "zapi",
                            "zapi_instance_id": world.zapi_instance,
                            "zapi_inbound_token_digest": hashlib.sha256(
                                world.zapi_token.encode("ascii")
                            ).hexdigest(),
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=world.uazapi_tenant,
                        api_credentials={"uazapi_instance_name": "credit-uazapi"},
                    ),
                    ClinicConfigTable(
                        tenant_id=world.evolution_tenant,
                        api_credentials={"evolution_instance_name": "credit-evolution"},
                    ),
                ]
            )
    finally:
        await database.close()


def new_world(client: Any) -> World:
    """Seed one tenant per ingress in the running app's database."""

    seeded = World(
        builder=uuid4(),
        api_tenant=uuid4(),
        api_key=secrets.token_urlsafe(24),
        api_session=uuid4(),
        jwt_tenant=uuid4(),
        jwt_session=uuid4(),
        meta_tenant=uuid4(),
        meta_phone=f"credit-meta-{uuid4().hex[:8]}",
        meta_secret=secrets.token_urlsafe(32),
        inactive_meta_tenant=uuid4(),
        inactive_meta_phone=f"credit-meta-{uuid4().hex[:8]}",
        inactive_meta_secret=secrets.token_urlsafe(32),
        zapi_tenant=uuid4(),
        zapi_token=_capability_token(),
        zapi_instance=f"3E{uuid4().hex[:10]}",
        uazapi_tenant=uuid4(),
        evolution_tenant=uuid4(),
    )
    client._submit(_seed(seeded))
    return seeded


@pytest.fixture
def world(client: Any) -> World:
    return new_world(client)


# ---------------------------------------------------------------------------
# Probes: the switch, request.state at route entry, SQL and the replay claim.
# ---------------------------------------------------------------------------


@pytest.fixture
def force_switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    return force


@contextmanager
def route_probe(
    app: Any, on_entry: Callable[[], None] | None = None
) -> Iterator[list[dict[str, Any]]]:
    """``request.state`` as every route sees it when its session dependency runs.

    Every webhook route and ``POST /v1/events`` resolve this dependency before
    their body runs, so an entry is the route's first moment.
    """

    entries: list[dict[str, Any]] = []

    async def probe(request: Request) -> AsyncIterator[AsyncSession]:
        entries.append(
            {
                "path": request.url.path,
                "switch": getattr(request.state, CREDIT_SWITCH_STATE_KEY, None),
                "identity": getattr(request.state, CREDIT_IDENTITY_STATE_KEY, None),
            }
        )
        if on_entry is not None:
            on_entry()
        session = get_db_session()
        try:
            yield session
        finally:
            await session.close()

    assert get_db_session_dependency not in app.dependency_overrides
    app.dependency_overrides[get_db_session_dependency] = probe
    try:
        yield entries
    finally:
        app.dependency_overrides.pop(get_db_session_dependency, None)


@pytest.fixture
def route_entries(test_app: Any) -> Iterator[list[dict[str, Any]]]:
    with route_probe(test_app) as entries:
        yield entries


@pytest.fixture
def sql(client: Any) -> Iterator[list[str]]:
    """Every statement the app's pool executes after start-up."""

    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    engine = get_db_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.fixture
def claims(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, UUID, str]]:
    seen: list[tuple[str, UUID, str]] = []
    original = webhook._claim_incoming_message

    def spy(source: str, tenant_id: UUID, message_id: str) -> bool:
        seen.append((source, tenant_id, message_id))
        return original(source, tenant_id, message_id)

    monkeypatch.setattr(webhook, "_claim_incoming_message", spy)
    return seen


def _credit_sql(statements: list[str]) -> list[str]:
    return [statement for statement in statements if "credit_" in statement.lower()]


async def _credit_row_counts() -> dict[str, int]:
    database = get_db_session()
    try:
        return {
            table: int(
                (
                    await database.execute(
                        select(func.count()).select_from(text(table))
                    )
                ).scalar_one()
            )
            for table in _CREDIT_TABLES
        }
    finally:
        await database.close()


# ---------------------------------------------------------------------------
# Request builders.
# ---------------------------------------------------------------------------


def _jwt(
    tenant_id: UUID,
    *,
    subject: UUID,
    builder_id: UUID | None = None,
    expired: bool = False,
) -> str:
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
        "sub": str(subject),
        "tenant_id": str(tenant_id),
        "role": "clinic_admin",
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(hours=1) if expired else now + timedelta(hours=1),
    }
    if builder_id is not None:
        claims["builder_id"] = str(builder_id)
    return jwt.encode(claims, settings.secret_key, algorithm="HS256")


def _tampered_jwt(tenant_id: UUID) -> str:
    """A token whose signature no longer matches its payload."""

    header, payload, signature = _jwt(tenant_id, subject=uuid4()).split(".")
    flipped = "A" if signature[0] != "A" else "B"
    return ".".join((header, payload, flipped + signature[1:]))


def _event_body(session_id: UUID, tenant_id: UUID | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": str(session_id),
        "event_type": "message.in",
        "payload": {"content": "Olá, quero agendar", "channel": "whatsapp"},
    }
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    return body


def _meta_body(phone_id: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "credit-waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": phone_id},
                            "statuses": [],
                        },
                    }
                ],
            }
        ],
    }


def _meta_post(client: Any, body: dict[str, Any], secret: str) -> Any:
    raw = json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        META,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


def _uazapi_body(instance: str) -> dict[str, Any]:
    return {
        "instanceName": instance,
        "message": {
            "messageid": f"credit-{uuid4().hex[:10]}",
            "text": "Olá",
            "fromMe": False,
            "sender_pn": "5511900000001@s.whatsapp.net",
        },
        "chat": {"wa_chatid": "5511900000001@s.whatsapp.net"},
    }


def _evolution_body(instance: str) -> dict[str, Any]:
    return {
        "event": "messages.upsert",
        "instance": instance,
        "data": {
            "key": {
                "remoteJid": "5511900000001@s.whatsapp.net",
                "fromMe": False,
                "id": f"credit-{uuid4().hex[:10]}",
            },
            "message": {"conversation": "Olá"},
            "pushName": "Paciente",
        },
    }


_REJECTIONS: dict[str, Callable[[Any, World], Any]] = {
    "events_missing_credentials": lambda c, w: c.post(
        EVENTS, json=_event_body(w.api_session)
    ),
    "events_unknown_api_key": lambda c, w: c.post(
        EVENTS,
        json=_event_body(w.api_session),
        headers={"X-API-Key": secrets.token_urlsafe(24)},
    ),
    "events_invalid_jwt": lambda c, w: c.post(
        EVENTS,
        json=_event_body(w.jwt_session),
        headers={"Authorization": f"Bearer {_tampered_jwt(w.jwt_tenant)}"},
    ),
    "events_expired_jwt": lambda c, w: c.post(
        EVENTS,
        json=_event_body(w.jwt_session),
        headers={
            "Authorization": f"Bearer {_jwt(w.jwt_tenant, subject=uuid4(), expired=True)}"
        },
    ),
    "events_cross_tenant_assertion": lambda c, w: c.post(
        EVENTS,
        json=_event_body(w.api_session, tenant_id=w.jwt_tenant),
        headers={"X-API-Key": w.api_key},
    ),
    "meta_bad_signature": lambda c, w: _meta_post(
        c, _meta_body(w.meta_phone), secrets.token_urlsafe(32)
    ),
    "meta_inactive_tenant": lambda c, w: _meta_post(
        c, _meta_body(w.inactive_meta_phone), w.inactive_meta_secret
    ),
    "zapi_non_canonical_token": lambda c, w: c.post(
        "/v1/webhook/zapi/" + w.zapi_token[:-1],
        json={"instanceId": w.zapi_instance, "messageId": "credit-zapi"},
    ),
    "zapi_unknown_token": lambda c, w: c.post(
        f"/v1/webhook/zapi/{_capability_token()}",
        json={"instanceId": w.zapi_instance, "messageId": "credit-zapi"},
    ),
    "zapi_instance_mismatch": lambda c, w: c.post(
        f"/v1/webhook/zapi/{w.zapi_token}",
        json={"instanceId": "3Eunknown", "messageId": "credit-zapi"},
    ),
    "uazapi_unknown_instance": lambda c, w: c.post(
        "/v1/webhook/uazapi", json=_uazapi_body("credit-unknown")
    ),
    "evolution_unknown_instance": lambda c, w: c.post(
        "/v1/webhook/evolution", json=_evolution_body("credit-unknown")
    ),
}


# ---------------------------------------------------------------------------
# Accepted requests expose exactly the authenticated identity.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effective", [True, False])
def test_an_api_key_event_exposes_its_tenant_identity(
    client: Any,
    world: World,
    route_entries: list[dict[str, Any]],
    force_switch: Callable[[bool], None],
    effective: bool,
) -> None:
    force_switch(effective)

    response = client.post(
        EVENTS,
        json=_event_body(world.api_session, tenant_id=world.api_tenant),
        headers={"X-API-Key": world.api_key},
    )

    assert response.status_code == 200, response.text
    assert route_entries == [
        {
            "path": EVENTS,
            "switch": effective,
            "identity": AuthenticatedIdentity("api_key", world.api_tenant),
        }
    ]
    identity = route_entries[0]["identity"]
    principal = client._submit(_derive(identity))
    assert principal.principal_id == f"api_key:{world.api_tenant}"
    assert (principal.builder_id, principal.authenticated) == (world.builder, True)


async def _derive(identity: AuthenticatedIdentity) -> Any:
    database = get_db_session()
    try:
        tenant = await read_tenant_builder(database, identity.tenant_id)
    finally:
        await database.close()
    assert tenant is not None
    return derive_credit_principal(identity, tenant)


def test_a_jwt_event_is_a_user_principal_whose_builder_comes_from_tenants(
    client: Any,
    world: World,
    route_entries: list[dict[str, Any]],
    force_switch: Callable[[bool], None],
) -> None:
    force_switch(True)
    subject = uuid4()
    # The token claims a builder; the tenant row has none: standalone wins.
    token = _jwt(world.jwt_tenant, subject=subject, builder_id=world.builder)

    response = client.post(
        EVENTS,
        json=_event_body(world.jwt_session),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    [entry] = route_entries
    assert entry["identity"] == AuthenticatedIdentity("jwt", world.jwt_tenant, subject)
    principal = client._submit(_derive(entry["identity"]))
    assert principal.principal_id == f"user:{subject}"
    assert principal.builder_id is None and principal.standalone


def test_a_signed_meta_delivery_exposes_the_hmac_tenant(
    client: Any,
    world: World,
    route_entries: list[dict[str, Any]],
    force_switch: Callable[[bool], None],
) -> None:
    force_switch(True)

    response = _meta_post(client, _meta_body(world.meta_phone), world.meta_secret)

    assert response.status_code == 200, response.text
    [entry] = route_entries
    assert entry == {
        "path": META,
        "switch": True,
        "identity": AuthenticatedIdentity("meta", world.meta_tenant),
    }
    principal = client._submit(_derive(entry["identity"]))
    assert principal.principal_id == f"meta:{world.meta_tenant}"
    assert principal.builder_id == world.builder


# ---------------------------------------------------------------------------
# Rejected requests: like main, nothing published, nothing written.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(_REJECTIONS))
def test_a_rejected_request_answers_like_main_and_writes_nothing(
    client: Any,
    world: World,
    route_entries: list[dict[str, Any]],
    sql: list[str],
    claims: list[tuple[str, UUID, str]],
    force_switch: Callable[[bool], None],
    case: str,
) -> None:
    answers = {}
    for effective in (False, True):
        force_switch(effective)
        response = _REJECTIONS[case](client, world)
        answers[effective] = (response.status_code, response.json())

    assert answers[True] == answers[False]
    status, body = answers[True]
    assert status >= 400 or body.get("skipped") == "unknown_instance", body
    assert all(entry["identity"] is None for entry in route_entries)
    assert claims == []
    assert _credit_sql(sql) == []
    assert client._submit(_credit_row_counts()) == dict.fromkeys(_CREDIT_TABLES, 0)


def test_a_cross_tenant_session_is_main_s_403_and_writes_no_credit_row(
    client: Any,
    world: World,
    route_entries: list[dict[str, Any]],
    sql: list[str],
    force_switch: Callable[[bool], None],
) -> None:
    """The API key passes; the session belongs to another tenant (route-level)."""

    answers = {}
    for effective in (False, True):
        force_switch(effective)
        response = client.post(
            EVENTS,
            json=_event_body(world.jwt_session),
            headers={"X-API-Key": world.api_key},
        )
        answers[effective] = (response.status_code, response.json())

    assert answers[True] == answers[False]
    assert answers[True][0] == 403
    assert answers[True][1]["error"] == "tenant_identity_mismatch"
    # Authenticated, so the identity is exposed; credit code still writes nothing.
    assert [entry["switch"] for entry in route_entries] == [False, True]
    assert _credit_sql(sql) == []
    assert client._submit(_credit_row_counts()) == dict.fromkeys(_CREDIT_TABLES, 0)


def test_the_credit_tables_exist_in_this_database(client: Any) -> None:
    """The row-count probe is not vacuous: every credit table is there."""

    assert len(_CREDIT_TABLES) == 15
    assert client._submit(_credit_row_counts()) == dict.fromkeys(_CREDIT_TABLES, 0)
