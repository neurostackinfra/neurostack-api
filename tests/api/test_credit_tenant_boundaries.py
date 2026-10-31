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
* each builder only ever sees its own figures, also through its clinics; a
  super admin reads the builder of the tenant it selects;
* negative proof: with the platform's resolver bypassed (it returns the
  requested tenant blindly), the route's own re-read still refuses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest
from fastapi import Depends, Query, Request
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.routes import credits as credit_routes
from src.models.tenant import Tenant
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
