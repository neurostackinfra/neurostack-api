"""Tenant boundaries of the credit read API (credit Group 6), SQLite.

The builder of every credit read is derived from the authenticated principal
(``tenants.builder_id`` re-read), never from the client.  Pinned here:

* a clinic of another builder and a tenant that does not exist answer the
  SAME ``403`` -- status, body and headers -- and the refusal happens before
  any ``credit_*`` or ``billing_*`` table is read (a statement listener on
  the engine sees none), so before any lock, order, ledger, outbox, storage
  or receipt;
* API keys and roles other than builder/super admin are refused before any
  SQL at all;
* a builder's JWT claim that no longer matches its tenant's stored builder
  (a reparent) is refused: the re-read wins;
* the one fallback: a builder's OWN tenant (no ``tenant_id``, or its own)
  linked to no builder reads the JWT claim's account; an own tenant linked
  to another builder, an absent own tenant and a clinic override linked to
  no builder are still refused (production 2026-11-10);
* that fallback re-reads the token's ``users`` row: a user since
  deactivated, deleted, demoted, moved to another builder or to another
  tenant is refused like an absent tenant, with no ``credit_*`` or
  ``billing_*`` SQL; and an unlinked tenant shared by builder users of two
  builders is refused to both (a second user of the same builder, or a
  ``clinic_admin`` user, keeps the fallback);
* each builder only ever sees its own figures, also through its clinics; a
  super admin reads the builder of the tenant it selects;
* negative proof: with the platform's resolver bypassed (it returns the
  requested tenant blindly), the route's own re-read still refuses.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, Query, Request
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context, pwd_context
from src.api.routes import credits as credit_routes
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories.database import (
    get_db_engine,
    get_db_session,
    get_db_session_dependency,
)
from src.services.credit_switch import CreditSwitch
from tests.api.test_credits import CREDITS, READ_ROUTES, Builders, _jwt, _seed

FORBIDDEN = {"detail": {"code": "tenant_override_forbidden"}}
SCOPE_FORBIDDEN = {"detail": {"code": "credit_scope_forbidden"}}


@pytest.fixture
def world(client: Any) -> Builders:
    seeded = Builders()
    client._submit(_seed(seeded))
    return seeded


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


# Headers that legitimately differ between any two answers (the clock, the
# request id, the caller's rate-limit counter).
_VOLATILE_HEADERS = {
    "date",
    "x-request-id",
    "x-response-time",
    "x-process-time",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}


@contextmanager
def _statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def record(_c: Any, _cur: Any, statement: str, *_a: Any) -> None:
        seen.append(statement)

    engine = get_db_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _financial(statements: list[str]) -> list[str]:
    return [s for s in statements if "credit_" in s or "billing_" in s]


def _headers(response: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _VOLATILE_HEADERS
    }


@pytest.mark.parametrize("route", READ_ROUTES)
def test_a_foreign_clinic_and_an_absent_tenant_are_indistinguishable(
    client: Any, world: Builders, switch: Callable[[bool], None], route: str
) -> None:
    with _statements() as statements:
        foreign = client.get(
            f"{CREDITS}/{route}",
            params={"tenant_id": str(world.indebted_tenant)},
            headers=world.rich_jwt,
        )
        absent = client.get(
            f"{CREDITS}/{route}",
            params={"tenant_id": str(uuid4())},
            headers=world.rich_jwt,
        )
    assert foreign.status_code == absent.status_code == 403
    assert foreign.json() == absent.json() == FORBIDDEN
    assert _headers(foreign) == _headers(absent)
    assert str(world.indebted_tenant) not in foreign.text
    # Refused before any credit or billing read (no lock, order, ledger,
    # outbox, storage or receipt is ever reached).
    assert statements, "the listener saw no SQL at all"
    assert _financial(statements) == []


@pytest.mark.parametrize(
    "headers",
    ["clinic_api_key", "clinic_admin", "unknown_role", "builder_without_claim"],
)
def test_other_principals_are_refused_before_any_sql(
    client: Any, world: Builders, switch: Callable[[bool], None], headers: str
) -> None:
    chosen = {
        "clinic_api_key": {"X-API-Key": world.clinic_key},
        "clinic_admin": _jwt(world.rich_clinic, role="clinic_admin"),
        "unknown_role": _jwt(world.rich_clinic, role="investor"),
        "builder_without_claim": _jwt(world.rich_tenant, role="builder"),
    }[headers]
    with _statements() as statements:
        responses = [
            client.get(f"{CREDITS}/{route}", headers=chosen) for route in READ_ROUTES
        ]
    expected = FORBIDDEN if headers == "builder_without_claim" else SCOPE_FORBIDDEN
    for response in responses:
        assert (response.status_code, response.json()) == (403, expected)
    # The API key's own authentication reads ``tenants``; nothing financial.
    assert _financial(statements) == []
    if headers != "clinic_api_key":
        assert statements == []


def test_each_builder_sees_only_its_own_figures(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    rich = client.get(f"{CREDITS}/summary", headers=world.rich_jwt).json()
    through_clinic = client.get(
        f"{CREDITS}/summary",
        params={"tenant_id": str(world.rich_clinic)},
        headers=world.rich_jwt,
    ).json()
    indebted = client.get(f"{CREDITS}/summary", headers=world.indebted_jwt).json()
    assert rich["balance"]["available_mc"] == 496_000
    assert through_clinic["balance"] == rich["balance"] | {
        "as_of": through_clinic["balance"]["as_of"]
    }
    assert (indebted["balance"]["available_mc"], indebted["debt"]["open_debt_mc"]) == (
        0,
        3_000,
    )
    # A builder id in the query is ignored (never a scope).
    ignored = client.get(
        f"{CREDITS}/balance",
        params={"builder_id": str(world.indebted)},
        headers=world.rich_jwt,
    ).json()
    assert ignored["available_mc"] == 496_000


def test_a_super_admin_reads_the_builder_of_the_selected_tenant(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    clinic = client.get(
        f"{CREDITS}/balance",
        params={"tenant_id": str(world.rich_clinic)},
        headers=world.super_admin,
    ).json()
    indebted = client.get(
        f"{CREDITS}/debt",
        params={"tenant_id": str(world.indebted_tenant)},
        headers=world.super_admin,
    ).json()
    assert clinic["available_mc"] == 496_000
    assert indebted["open_debt_mc"] == 3_000
    absent = client.get(
        f"{CREDITS}/balance",
        params={"tenant_id": str(uuid4())},
        headers=world.super_admin,
    )
    assert (absent.status_code, absent.json()) == (403, FORBIDDEN)


async def _reparent(tenant_id: UUID, builder_id: UUID) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            await database.execute(
                update(Tenant)
                .where(Tenant.id == tenant_id)
                .values(builder_id=builder_id)
            )
    finally:
        await database.close()


def test_the_stored_builder_wins_over_the_jwt_claim(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    """A clinic moved to another builder: the old builder's token, still
    naming it as its own tenant, reads nothing of either account."""

    headers = world.builder(world.rich, world.rich_clinic)
    assert client.get(f"{CREDITS}/balance", headers=headers).status_code == 200
    client._submit(_reparent(world.rich_clinic, world.indebted))
    with _statements() as statements:
        response = client.get(f"{CREDITS}/balance", headers=headers)
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert _financial(statements) == []


def test_negative_proof_the_route_rechecks_the_stored_builder(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the platform resolver bypassed (any requested tenant accepted),
    the credit scope's own re-read of ``tenants.builder_id`` still refuses a
    foreign clinic; and without that re-read the foreign figures WOULD leak
    -- so the guard, not the fixture, is what refuses."""

    async def blind(
        _request: Any, _tenant: Any, requested: UUID | None, _session: Any
    ) -> UUID:
        assert requested is not None
        return requested

    monkeypatch.setattr(credit_routes, "resolve_effective_tenant", blind)
    params = {"tenant_id": str(world.indebted_tenant)}
    refused = client.get(f"{CREDITS}/debt", params=params, headers=world.rich_jwt)
    assert (refused.status_code, refused.json()) == (403, FORBIDDEN)

    original = credit_routes.resolve_credit_scope

    async def without_recheck(
        request: Request,
        tenant_id: Optional[UUID] = Query(default=None),
        tenant: TenantContext = Depends(get_tenant_context),
        session: AsyncSession = Depends(get_db_session_dependency),
    ) -> credit_routes.CreditScope:
        effective = await blind(request, tenant, tenant_id, session)
        found = await credit_routes.CreditRepository(session).tenant_scope(effective)
        assert found is not None
        return credit_routes.CreditScope(effective, found.builder_id)

    target = client.application
    target.dependency_overrides[original] = without_recheck
    try:
        leaked = client.get(f"{CREDITS}/debt", params=params, headers=world.rich_jwt)
    finally:
        target.dependency_overrides.pop(original, None)
    assert leaked.status_code == 200
    assert leaked.json()["open_debt_mc"] == 3_000


# ---------------------------------------------------------------------------
# A builder's own (home) tenant linked to no builder (production 2026-11-10:
# the builder user's home tenant has ``tenants.builder_id`` NULL).  Only the
# OWN tenant falls back to the JWT claim; a clinic override never does.
# ---------------------------------------------------------------------------


async def _home_tenant(builder_id: UUID | None) -> UUID:
    """A tenant row like a builder user's home tenant, linked to
    ``builder_id`` (``None``: to no builder, the production shape)."""

    tenant_id = uuid4()
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                Tenant(
                    id=tenant_id,
                    name=f"Home tenant {tenant_id.hex[:6]}",
                    slug=f"home-{tenant_id.hex}",
                    api_key_hash=pwd_context.hash(secrets.token_urlsafe(16)),
                    builder_id=builder_id,
                    config={},
                )
            )
    finally:
        await database.close()
    return tenant_id


async def _builder_user(
    tenant_id: UUID, builder_id: UUID | None, role: UserRole = UserRole.builder
) -> UUID:
    """A current ``users`` row (role builder unless ``role`` says otherwise)
    on ``tenant_id`` (the row the login of a user signs its token from);
    returns its id, the token's ``sub``."""

    user_id = uuid4()
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                User(
                    id=user_id,
                    tenant_id=tenant_id,
                    email=f"builder-{user_id.hex}@example.com",
                    password_hash=pwd_context.hash(secrets.token_urlsafe(16)),
                    role=role,
                    builder_id=builder_id,
                    is_active=True,
                )
            )
    finally:
        await database.close()
    return user_id


# How a token's ``users`` row can go stale after the token was signed.
STALE = ("deactivated", "moved_builder", "moved_tenant", "deleted", "demoted")


async def _make_stale(
    user_id: UUID, stale: str, *, builder_id: UUID, tenant_id: UUID
) -> None:
    """Make ``user_id`` stale as ``stale`` says; ``builder_id`` and
    ``tenant_id`` are the builder and tenant it is moved to."""

    database = get_db_session()
    try:
        async with database.begin():
            if stale == "deleted":
                await database.execute(delete(User).where(User.id == user_id))
                return
            values: dict[str, Any] = {
                "deactivated": {"is_active": False},
                "moved_builder": {"builder_id": builder_id},
                "moved_tenant": {"tenant_id": tenant_id},
                "demoted": {"role": UserRole.clinic_admin},
            }[stale]
            await database.execute(
                update(User).where(User.id == user_id).values(**values)
            )
    finally:
        await database.close()


def _users_reads(statements: list[str]) -> list[str]:
    return [s for s in statements if "FROM users" in s]


def _home_builder(client: Any, builder_id: UUID, home: UUID) -> dict[str, str]:
    """A builder user's token on its home tenant, with its ``users`` row."""

    user = client._submit(_builder_user(home, builder_id))
    return _jwt(home, role="builder", builder_id=builder_id, subject=user)


@pytest.fixture
def scopes(monkeypatch: pytest.MonkeyPatch) -> list[credit_routes.CreditScope]:
    """Every scope a credit snapshot is built for: which builder's account
    a request actually read, not only its status."""

    seen: list[credit_routes.CreditScope] = []

    class Recording(credit_routes.CreditSnapshot):
        def __init__(self, session: Any, scope: credit_routes.CreditScope) -> None:
            seen.append(scope)
            super().__init__(session, scope)

    monkeypatch.setattr(credit_routes, "CreditSnapshot", Recording)
    return seen


def _lot_ids(response: Any) -> set[str]:
    assert response.status_code == 200, response.text
    return {lot["lot_id"] for lot in response.json()["lots"]}


@pytest.mark.parametrize("own_tenant_param", [False, True])
def test_a_builder_reads_its_own_account_through_a_home_tenant_linked_to_no_builder(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
    own_tenant_param: bool,
) -> None:
    """No ``tenant_id`` (the /billing page), or ``tenant_id`` equal to the
    token's own tenant: the account is the JWT claim's builder."""

    rich_lots = _lot_ids(client.get(f"{CREDITS}/lots", headers=world.rich_jwt))
    assert rich_lots
    home = client._submit(_home_tenant(None))
    headers = _home_builder(client, world.rich, home)
    params = {"tenant_id": str(home)} if own_tenant_param else {}
    scopes.clear()

    responses = {
        route: client.get(f"{CREDITS}/{route}", params=params, headers=headers)
        for route in READ_ROUTES
    }
    for route, response in responses.items():
        assert response.status_code == 200, (route, response.text)
    # The builder actually read: the token's, through the home tenant, for
    # every route that builds a snapshot (the catalog builds none).
    assert scopes
    assert set(scopes) == {credit_routes.CreditScope(home, world.rich)}
    summary = responses["summary"].json()
    assert summary["balance"]["available_mc"] == 496_000
    assert summary["state"]["pending_reasons"] == []
    assert _lot_ids(responses["lots"]) == rich_lots


def test_a_builder_whose_own_tenant_is_linked_to_another_builder_is_refused(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    """A conflict, not a fallback: the stored builder wins and the answer is
    the absent tenant's, before any credit or billing read."""

    home = client._submit(_home_tenant(world.indebted))
    # A current users row: the conflict, not a missing row, refuses.
    headers = _home_builder(client, world.rich, home)
    absent = world.builder(world.rich, uuid4())
    with _statements() as statements:
        for route in READ_ROUTES:
            for params in ({}, {"tenant_id": str(home)}):
                conflict = client.get(
                    f"{CREDITS}/{route}", params=params, headers=headers
                )
                missing = client.get(
                    f"{CREDITS}/{route}", params=params, headers=absent
                )
                assert (conflict.status_code, conflict.json()) == (403, FORBIDDEN)
                assert (missing.status_code, missing.json()) == (403, FORBIDDEN)
                assert _headers(conflict) == _headers(missing)
    assert _financial(statements) == []
    assert scopes == []


def test_a_builder_whose_own_tenant_row_is_absent_is_refused(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    gone = uuid4()
    headers = world.builder(world.rich, gone)
    with _statements() as statements:
        for route in READ_ROUTES:
            for params in ({}, {"tenant_id": str(gone)}):
                response = client.get(
                    f"{CREDITS}/{route}", params=params, headers=headers
                )
                assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert _financial(statements) == []
    assert scopes == []


@pytest.mark.parametrize("bypass_resolver", [False, True])
def test_a_clinic_override_linked_to_no_builder_stays_refused(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
    monkeypatch: pytest.MonkeyPatch,
    bypass_resolver: bool,
) -> None:
    """The home-tenant fallback never covers a clinic override: a clinic
    linked to no builder is refused, from the token's linked tenant and from
    a home tenant linked to no builder alike; with the platform resolver
    bypassed (any requested tenant accepted) the route's own check still
    refuses it."""

    home = client._submit(_home_tenant(None))
    unlinked_clinic = client._submit(_home_tenant(None))
    # A current builder user of the home tenant: served without an override.
    home_headers = _home_builder(client, world.rich, home)
    assert client.get(f"{CREDITS}/summary", headers=home_headers).status_code == 200
    scopes.clear()
    if bypass_resolver:

        async def blind(
            _request: Any, _tenant: Any, requested: UUID | None, _session: Any
        ) -> UUID:
            assert requested is not None
            return requested

        monkeypatch.setattr(credit_routes, "resolve_effective_tenant", blind)
    with _statements() as statements:
        for headers in (world.rich_jwt, home_headers):
            for clinic in (unlinked_clinic, world.standalone):
                for route in READ_ROUTES:
                    response = client.get(
                        f"{CREDITS}/{route}",
                        params={"tenant_id": str(clinic)},
                        headers=headers,
                    )
                    assert (response.status_code, response.json()) == (
                        403,
                        FORBIDDEN,
                    ), (route, clinic)
    assert _financial(statements) == []
    assert scopes == []


def test_a_clinic_override_of_its_own_linked_clinic_is_unchanged(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    home = client._submit(_home_tenant(None))
    for headers in (world.rich_jwt, world.builder(world.rich, home)):
        scopes.clear()
        response = client.get(
            f"{CREDITS}/summary",
            params={"tenant_id": str(world.rich_clinic)},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["balance"]["available_mc"] == 496_000
        assert scopes == [credit_routes.CreditScope(world.rich_clinic, world.rich)]


def test_a_home_tenant_linked_to_no_builder_never_reads_another_builder(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    """Two builders, each on its own unlinked home tenant, each read only
    their own account; a builder id in the query is ignored; and another
    builder's clinic stays refused from that home tenant.  (Two builders on
    the SAME unlinked tenant are both refused: the test below.)"""

    rich_lots = _lot_ids(client.get(f"{CREDITS}/lots", headers=world.rich_jwt))
    home = client._submit(_home_tenant(None))
    indebted_home = client._submit(_home_tenant(None))
    rich = _home_builder(client, world.rich, home)
    indebted = _home_builder(client, world.indebted, indebted_home)
    scopes.clear()

    indebted_summary = client.get(f"{CREDITS}/summary", headers=indebted)
    assert indebted_summary.status_code == 200, indebted_summary.text
    body = indebted_summary.json()
    assert (body["balance"]["available_mc"], body["debt"]["open_debt_mc"]) == (
        0,
        3_000,
    )
    indebted_lots = _lot_ids(client.get(f"{CREDITS}/lots", headers=indebted))
    assert indebted_lots.isdisjoint(rich_lots)
    tampered = client.get(
        f"{CREDITS}/lots",
        params={"builder_id": str(world.rich)},
        headers=indebted,
    )
    assert _lot_ids(tampered) == indebted_lots
    tampered_summary = client.get(
        f"{CREDITS}/summary",
        params={"builder_id": str(world.indebted)},
        headers=rich,
    )
    assert tampered_summary.json()["balance"]["available_mc"] == 496_000
    assert {scope.builder_id for scope in scopes} == {world.indebted, world.rich}
    assert [s for s in scopes if s.builder_id == world.rich] == [
        credit_routes.CreditScope(home, world.rich)
    ]

    for route in ("summary", "lots"):
        foreign = client.get(
            f"{CREDITS}/{route}",
            params={"tenant_id": str(world.indebted_tenant)},
            headers=rich,
        )
        assert (foreign.status_code, foreign.json()) == (403, FORBIDDEN)


def test_an_unlinked_tenant_shared_by_two_builders_is_refused_to_both(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    """Builder users of two different builders on ONE unlinked tenant: it
    is no single builder's home, so both tokens get the absent tenant's
    403, before any credit or billing read (owner decision 2026-11-10)."""

    home = client._submit(_home_tenant(None))
    rich = _home_builder(client, world.rich, home)
    # Served while the tenant has only the rich builder's user.
    assert client.get(f"{CREDITS}/summary", headers=rich).status_code == 200
    indebted = _home_builder(client, world.indebted, home)
    absent = world.builder(world.rich, uuid4())
    scopes.clear()
    requests = 0
    with _statements() as statements:
        for route in READ_ROUTES:
            missing = client.get(f"{CREDITS}/{route}", headers=absent)
            assert (missing.status_code, missing.json()) == (403, FORBIDDEN)
            for headers in (rich, indebted):
                for params in ({}, {"tenant_id": str(home)}):
                    refused = client.get(
                        f"{CREDITS}/{route}", params=params, headers=headers
                    )
                    requests += 1
                    assert (refused.status_code, refused.json()) == (
                        403,
                        FORBIDDEN,
                    ), (route, params)
                    assert _headers(refused) == _headers(missing)
    assert _financial(statements) == []
    assert scopes == []
    # One users statement per refused request: the token's row and the
    # shared-tenant EXISTS are a single SELECT (the absent tenant reads none).
    assert len(_users_reads(statements)) == requests


def test_a_builder_user_of_no_builder_on_the_unlinked_tenant_blocks_the_fallback(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    """A builder-role user whose ``builder_id`` is NULL on the tenant is "of
    no builder", hence not the token's builder: the shared-tenant check is
    ``IS DISTINCT FROM`` (a plain ``!=`` would let NULL through)."""

    home = client._submit(_home_tenant(None))
    headers = _home_builder(client, world.rich, home)
    assert client.get(f"{CREDITS}/summary", headers=headers).status_code == 200
    client._submit(_builder_user(home, None))
    scopes.clear()
    with _statements() as statements:
        response = client.get(f"{CREDITS}/summary", headers=headers)
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert _financial(statements) == []
    assert scopes == []


def test_a_second_user_of_the_same_builder_or_a_clinic_admin_keeps_the_fallback(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    home = client._submit(_home_tenant(None))
    first = _home_builder(client, world.rich, home)
    second = _home_builder(client, world.rich, home)
    client._submit(_builder_user(home, None, UserRole.clinic_admin))
    scopes.clear()
    with _statements() as statements:
        for headers in (first, second):
            response = client.get(f"{CREDITS}/summary", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["balance"]["available_mc"] == 496_000
    assert scopes == [credit_routes.CreditScope(home, world.rich)] * 2
    assert len(_users_reads(statements)) == 2


# ---------------------------------------------------------------------------
# The fallback re-reads the token's ``users`` row: a stale token (the user
# deactivated, deleted, demoted, moved to another builder or tenant since it
# was signed) is the absent tenant's 403, before any credit or billing read.
# The linked-tenant path never reads ``users``.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stale", STALE)
def test_a_stale_token_on_an_unlinked_home_tenant_is_refused(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
    stale: str,
) -> None:
    home = client._submit(_home_tenant(None))
    user = client._submit(_builder_user(home, world.rich))
    headers = _jwt(home, role="builder", builder_id=world.rich, subject=user)
    # Served while the users row is current: the refusal below is the
    # staleness, not the fixture.
    served = client.get(f"{CREDITS}/summary", headers=headers)
    assert served.status_code == 200, served.text
    assert served.json()["balance"]["available_mc"] == 496_000
    client._submit(
        _make_stale(user, stale, builder_id=world.indebted, tenant_id=world.rich_tenant)
    )
    absent = world.builder(world.rich, uuid4())
    scopes.clear()
    with _statements() as statements:
        for route in READ_ROUTES:
            missing = client.get(f"{CREDITS}/{route}", headers=absent)
            for params in ({}, {"tenant_id": str(home)}):
                refused = client.get(
                    f"{CREDITS}/{route}", params=params, headers=headers
                )
                assert (refused.status_code, refused.json()) == (403, FORBIDDEN), (
                    route,
                    params,
                )
                assert (missing.status_code, missing.json()) == (403, FORBIDDEN)
                assert _headers(refused) == _headers(missing)
    # The users row was re-read, and nothing financial.
    assert _users_reads(statements)
    assert _financial(statements) == []
    assert scopes == []


def test_a_token_whose_sub_never_was_a_user_is_refused(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    scopes: list[credit_routes.CreditScope],
) -> None:
    """A builder token on its unlinked home tenant whose ``sub`` names no
    user (never logged in through ``/v1/auth/login``) is not served."""

    home = client._submit(_home_tenant(None))
    headers = world.builder(world.rich, home)
    absent = world.builder(world.rich, uuid4())
    with _statements() as statements:
        refused = client.get(f"{CREDITS}/summary", headers=headers)
        missing = client.get(f"{CREDITS}/summary", headers=absent)
    assert (refused.status_code, refused.json()) == (403, FORBIDDEN)
    assert _headers(refused) == _headers(missing)
    assert _users_reads(statements)
    assert _financial(statements) == []
    assert scopes == []


def test_the_linked_tenant_path_never_reads_users(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
) -> None:
    """The stale-claim re-read is the fallback's only: a builder's linked
    own tenant and its linked clinic are served with no ``users`` read."""

    with _statements() as statements:
        own = client.get(f"{CREDITS}/balance", headers=world.rich_jwt)
        clinic = client.get(
            f"{CREDITS}/balance",
            params={"tenant_id": str(world.rich_clinic)},
            headers=world.rich_jwt,
        )
    assert own.status_code == clinic.status_code == 200
    assert _users_reads(statements) == []
