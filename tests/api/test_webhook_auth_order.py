"""Authentication order the credit path builds on (neurostack-credit-system 1b).

Group 3 writes ingress claims; it relies on the order ``main`` already has,
pinned here on the real app with the switch forced on (and off):

* every ingress captures the switch exactly once, at entry: after
  ``AuthMiddleware`` for ``POST /v1/events`` (so a 401 captures nothing),
  before any lookup for the webhooks;
* each ingress authenticates before the replay claim: the Z-API digest and
  instance check, the Meta HMAC (in ``TenantIdentityMiddleware``), the
  Uazapi/Evolution instance lookup; a rejected delivery never reaches the
  claim;
* the identity reaches ``request.state`` only after its check passed: the
  Meta HMAC tenant after every envelope/receiver/activity/assertion check,
  the events tenant after the tenant assertion check; a rejection publishes
  nothing downstream and answers the same with the switch off or on;
* a flip of the process verdict in the middle of the authentication does not
  change the value the route sees;
* Uazapi/Evolution reach the claim knowing only the instance name: their
  principal is ``authenticated=False``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.middleware import tenant_identity
from src.api.middleware.auth import APIKeyStore, TenantContext
from src.api.middleware.tenant_identity import TenantIdentityMiddleware
from src.api.routes import webhook
from src.services import credit_switch
from src.services.credit_principal import (
    CREDIT_IDENTITY_STATE_KEY,
    AuthenticatedIdentity,
    TenantBuilder,
    derive_credit_principal,
)
from src.services.credit_switch import CREDIT_SWITCH_STATE_KEY, CreditSwitch
from tests.api.test_credit_principal_authorization import (
    EVENTS,
    META,
    World,
    _capability_token,
    _event_body,
    _evolution_body,
    _jwt,
    _meta_body,
    _meta_post,
    _uazapi_body,
    new_world,
    route_probe,
)

# ---------------------------------------------------------------------------
# The real app: an ordered trace of capture, auth lookups, route and claim.
# ---------------------------------------------------------------------------


@pytest.fixture
def world(client: Any) -> World:
    return new_world(client)


@pytest.fixture
def force_switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    return force


@pytest.fixture
def trace(monkeypatch: pytest.MonkeyPatch, test_app: Any) -> Iterator[list[str]]:
    steps: list[str] = []
    switch_read = credit_switch.credit_switch_effective

    def read_switch() -> bool:
        steps.append("switch")
        return switch_read()

    validate_api_key = APIKeyStore.validate_api_key

    async def api_key_lookup(_cls: Any, api_key: str) -> Any:
        steps.append("auth:api_key")
        return await validate_api_key(api_key)

    load_meta_candidates = tenant_identity._load_meta_candidates

    async def meta_lookup(receiver_ids: set[str]) -> Any:
        steps.append("auth:meta_hmac")
        return await load_meta_candidates(receiver_ids)

    resolve_zapi = webhook._resolve_tenant_by_zapi_digest

    async def zapi_lookup(digest: str, session: Any) -> Any:
        steps.append("auth:zapi_digest")
        return await resolve_zapi(digest, session)

    resolve_instance = webhook._resolve_tenant_by_credential

    async def instance_lookup(key: str, value: str, session: Any) -> Any:
        steps.append(f"route:{key}")
        return await resolve_instance(key, value, session)

    def claim(source: str, tenant_id: UUID, message_id: str) -> bool:
        steps.append(f"claim:{source}")
        return False  # a duplicate: stops before any downstream effect

    monkeypatch.setattr(credit_switch, "credit_switch_effective", read_switch)
    monkeypatch.setattr(APIKeyStore, "validate_api_key", classmethod(api_key_lookup))
    monkeypatch.setattr(tenant_identity, "_load_meta_candidates", meta_lookup)
    monkeypatch.setattr(webhook, "_resolve_tenant_by_zapi_digest", zapi_lookup)
    monkeypatch.setattr(webhook, "_resolve_tenant_by_credential", instance_lookup)
    monkeypatch.setattr(webhook, "_claim_incoming_message", claim)
    with route_probe(test_app, on_entry=lambda: steps.append("route")):
        yield steps


_ORDER: dict[str, tuple[Callable[[Any, World], Any], int, list[str]]] = {
    "events_api_key": (
        lambda c, w: c.post(
            EVENTS,
            json=_event_body(w.api_session, tenant_id=w.api_tenant),
            headers={"X-API-Key": w.api_key},
        ),
        200,
        ["auth:api_key", "switch", "route"],
    ),
    "events_unknown_api_key": (
        lambda c, w: c.post(
            EVENTS,
            json=_event_body(w.api_session),
            headers={"X-API-Key": "credit-order-unknown-key"},
        ),
        401,
        ["auth:api_key"],
    ),
    "events_jwt": (
        lambda c, w: c.post(
            EVENTS,
            json=_event_body(w.jwt_session),
            headers={"Authorization": f"Bearer {_jwt(w.jwt_tenant, subject=uuid4())}"},
        ),
        200,
        ["switch", "route"],
    ),
    "meta_signed": (
        lambda c, w: _meta_post(c, _meta_body(w.meta_phone), w.meta_secret),
        200,
        ["switch", "auth:meta_hmac", "route"],
    ),
    "meta_bad_signature": (
        lambda c, w: _meta_post(c, _meta_body(w.meta_phone), "not-the-secret"),
        401,
        ["switch", "auth:meta_hmac"],
    ),
    "zapi_valid": (
        lambda c, w: c.post(
            f"/v1/webhook/zapi/{w.zapi_token}",
            json={"instanceId": w.zapi_instance, "messageId": "credit-order"},
        ),
        200,
        ["switch", "route", "auth:zapi_digest", "claim:zapi"],
    ),
    "zapi_unknown_token": (
        lambda c, w: c.post(
            f"/v1/webhook/zapi/{_capability_token()}",
            json={"instanceId": w.zapi_instance, "messageId": "credit-order"},
        ),
        401,
        ["switch", "route", "auth:zapi_digest"],
    ),
    "zapi_non_canonical_token": (
        lambda c, w: c.post(
            "/v1/webhook/zapi/" + w.zapi_token[:-1],
            json={"instanceId": w.zapi_instance, "messageId": "credit-order"},
        ),
        401,
        ["switch", "route"],
    ),
    "uazapi_known_instance": (
        lambda c, w: c.post("/v1/webhook/uazapi", json=_uazapi_body("credit-uazapi")),
        200,
        ["switch", "route", "route:uazapi_instance_name", "claim:uazapi"],
    ),
    "uazapi_unknown_instance": (
        lambda c, w: c.post("/v1/webhook/uazapi", json=_uazapi_body("credit-nope")),
        200,
        ["switch", "route", "route:uazapi_instance_name"],
    ),
    "evolution_known_instance": (
        lambda c, w: c.post(
            "/v1/webhook/evolution", json=_evolution_body("credit-evolution")
        ),
        200,
        ["switch", "route", "route:evolution_instance_name", "claim:evolution"],
    ),
    "evolution_unknown_instance": (
        lambda c, w: c.post(
            "/v1/webhook/evolution", json=_evolution_body("credit-nope")
        ),
        200,
        ["switch", "route", "route:evolution_instance_name"],
    ),
}


@pytest.mark.parametrize("effective", [True, False])
@pytest.mark.parametrize("case", sorted(_ORDER))
def test_capture_auth_and_claim_order(
    client: Any,
    world: World,
    trace: list[str],
    force_switch: Callable[[bool], None],
    case: str,
    effective: bool,
) -> None:
    force_switch(effective)
    send, status, expected = _ORDER[case]

    response = send(client, world)

    assert response.status_code == status, response.text
    assert trace == expected
    if any(step.startswith("claim:") for step in expected):
        assert response.json() == {"ok": True, "skipped": "duplicate_message"}


def test_instance_routing_reaches_the_claim_unauthenticated(
    client: Any,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    force_switch: Callable[[bool], None],
) -> None:
    """No secret is presented, yet the claim is reached: authenticated=False."""

    force_switch(True)
    claimed: list[tuple[str, UUID]] = []

    def claim(source: str, tenant_id: UUID, _message_id: str) -> bool:
        claimed.append((source, tenant_id))
        return False

    monkeypatch.setattr(webhook, "_claim_incoming_message", claim)
    client.post("/v1/webhook/uazapi", json=_uazapi_body("credit-uazapi"))
    client.post("/v1/webhook/evolution", json=_evolution_body("credit-evolution"))

    assert claimed == [
        ("uazapi", world.uazapi_tenant),
        ("evolution", world.evolution_tenant),
    ]
    for kind, tenant_id in claimed:
        principal = derive_credit_principal(
            AuthenticatedIdentity(kind, tenant_id),  # type: ignore[arg-type]
            TenantBuilder(tenant_id, None, True),
        )
        assert principal.authenticated is False
        assert principal.principal_id == f"{kind}:{tenant_id}"


@pytest.mark.parametrize("at_entry", [True, False])
def test_a_flip_during_the_meta_hmac_does_not_reach_the_route(
    client: Any,
    world: World,
    test_app: Any,
    monkeypatch: pytest.MonkeyPatch,
    force_switch: Callable[[bool], None],
    at_entry: bool,
) -> None:
    force_switch(at_entry)
    load_meta_candidates = tenant_identity._load_meta_candidates

    async def flip_then_lookup(receiver_ids: set[str]) -> Any:
        force_switch(not at_entry)
        return await load_meta_candidates(receiver_ids)

    monkeypatch.setattr(tenant_identity, "_load_meta_candidates", flip_then_lookup)
    with route_probe(test_app) as entries:
        first = _meta_post(client, _meta_body(world.meta_phone), world.meta_secret)
        monkeypatch.setattr(
            tenant_identity, "_load_meta_candidates", load_meta_candidates
        )
        second = _meta_post(client, _meta_body(world.meta_phone), world.meta_secret)

    assert (first.status_code, second.status_code) == (200, 200)
    assert [entry["switch"] for entry in entries] == [at_entry, not at_entry]


# ---------------------------------------------------------------------------
# TenantIdentityMiddleware alone: identity only after every check passed.
# ---------------------------------------------------------------------------

_TENANT = UUID("3c4d5e6f-0000-4000-8000-0000000c0de1")
_PHONE = "credit-order-phone"
_SECRET = "credit-order-meta-test-secret"


def _isolated_app(downstream: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/webhook/meta")
    @app.post("/v1/events")
    async def endpoint(request: Request) -> dict[str, Any]:
        downstream.append(
            {
                "switch": getattr(request.state, CREDIT_SWITCH_STATE_KEY, None),
                "identity": getattr(request.state, CREDIT_IDENTITY_STATE_KEY, None),
            }
        )
        return {"ok": True}

    app.add_middleware(TenantIdentityMiddleware)
    return app


class _Authenticated:
    """Stands in for AuthMiddleware: a TenantContext already on the scope."""

    def __init__(self, app: Any, *, tenant: TenantContext | None) -> None:
        self.app = app
        self.tenant = tenant

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self.tenant is not None:
            scope.setdefault("state", {})["tenant"] = self.tenant
        await self.app(scope, receive, send)


def _signed(raw: bytes, secret: str = _SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "content-type": "application/json",
        "x-hub-signature-256": f"sha256={digest}",
    }


def _meta_raw(**overrides: Any) -> bytes:
    body = _meta_body(overrides.pop("phone", _PHONE))
    body.update(overrides)
    return json.dumps(body).encode()


_ASSERTS_OTHER_TENANT = _meta_raw(tenant_id=str(uuid4()))
_META_CASES: dict[str, tuple[bytes, dict[str, str], bool, int]] = {
    # name: (raw body, headers, candidate active, expected status)
    "valid": (_meta_raw(), _signed(_meta_raw()), True, 200),
    "bad_signature": (_meta_raw(), _signed(_meta_raw(), "other-secret"), True, 401),
    "missing_signature": (_meta_raw(), {"content-type": "application/json"}, True, 401),
    "malformed_envelope": (b"{not json", _signed(b"{not json"), True, 400),
    "wrong_object": (
        _meta_raw(object="page"),
        _signed(_meta_raw(object="page")),
        True,
        403,
    ),
    "inactive_tenant": (_meta_raw(), _signed(_meta_raw()), False, 403),
    "tenant_assertion_mismatch": (
        _ASSERTS_OTHER_TENANT,
        _signed(_ASSERTS_OTHER_TENANT),
        True,
        403,
    ),
    "unknown_receiver": (
        _meta_raw(phone="credit-order-other"),
        _signed(_meta_raw(phone="credit-order-other")),
        True,
        403,
    ),
}


@pytest.mark.parametrize("case", sorted(_META_CASES))
async def test_meta_identity_is_published_only_after_every_check(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    raw, headers, active, status = _META_CASES[case]

    async def candidates(_receiver_ids: set[str]) -> list[Any]:
        return [(_PHONE, _TENANT, active, {"meta_app_secret": _SECRET})]

    monkeypatch.setattr(tenant_identity, "_load_meta_candidates", candidates)
    answers = {}
    for effective in (False, True):
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)
        downstream: list[dict[str, Any]] = []
        async with AsyncClient(
            transport=ASGITransport(app=_isolated_app(downstream)),
            base_url="http://test",
        ) as client:
            response = await client.post(META, content=raw, headers=headers)
        answers[effective] = (response.status_code, response.json(), downstream)

    assert answers[False][:2] == answers[True][:2]
    assert answers[True][0] == status
    if status == 200:
        assert answers[True][2] == [
            {"switch": True, "identity": AuthenticatedIdentity("meta", _TENANT)}
        ]
        assert answers[False][2] == [
            {"switch": False, "identity": AuthenticatedIdentity("meta", _TENANT)}
        ]
    else:
        assert answers[True][2] == answers[False][2] == []


_SUBJECT = uuid4()
_EVENT_CASES: dict[str, tuple[TenantContext | None, bytes, int, Any]] = {
    # name: (authenticated context, raw body, status, identity downstream)
    "api_key_same_tenant": (
        TenantContext(tenant_id=_TENANT, api_key="validated"),
        json.dumps({"tenant_id": str(_TENANT)}).encode(),
        200,
        AuthenticatedIdentity("api_key", _TENANT),
    ),
    "api_key_no_assertion": (
        TenantContext(tenant_id=_TENANT, api_key="validated"),
        b"{}",
        200,
        AuthenticatedIdentity("api_key", _TENANT),
    ),
    "jwt": (
        TenantContext(tenant_id=_TENANT, user_id=_SUBJECT, authentication_mode="jwt"),
        b"{}",
        200,
        AuthenticatedIdentity("jwt", _TENANT, _SUBJECT),
    ),
    "cross_tenant_assertion": (
        TenantContext(tenant_id=_TENANT, api_key="validated"),
        json.dumps({"tenant_id": str(uuid4())}).encode(),
        403,
        None,
    ),
    "invalid_json": (
        TenantContext(tenant_id=_TENANT, api_key="validated"),
        b"[not-an-object]",
        400,
        None,
    ),
    "unauthenticated": (None, b"{}", 200, "absent"),
}


@pytest.mark.parametrize("case", sorted(_EVENT_CASES))
async def test_events_identity_is_published_only_after_the_assertion_check(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    tenant, raw, status, identity = _EVENT_CASES[case]
    for effective in (False, True):
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)
        downstream: list[dict[str, Any]] = []
        app = _Authenticated(_isolated_app(downstream), tenant=tenant)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                EVENTS, content=raw, headers={"content-type": "application/json"}
            )

        assert response.status_code == status
        if status != 200:
            assert downstream == []
        elif identity == "absent":
            assert downstream == [{"switch": effective, "identity": None}]
        else:
            assert downstream == [{"switch": effective, "identity": identity}]
