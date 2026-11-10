"""Courtesy credits over HTTP (creditos-cortesia Group 3), SQLite.

The real application (lifespan, middleware, SQLite ``:memory:``) with the
credit switch forced effective and the switch READING patched per test (the
courtesy gate judges it: SQLite never has ``alembic_version``, so a test
opens the gate with the head of this image's chain, or closes it with
``045``).  The three routes of ``/v1/admin/credits/courtesy-grants`` against
the DESIGN's Success Criteria:

* "concessão": a super admin's JWT grant makes exactly one ``courtesy`` lot
  of ``credits x 1000`` mc expiring at the start of the civil day of the
  grant + 30 days, one ``grant`` event with actor and reason, and no
  ``billing_*`` row;
* "recusas": every malformed body or ``Idempotency-Key`` is ``422`` with
  nothing written; an absent builder is ``404 builder_not_found``; an
  inactive one ``409 builder_inactive``;
* "autoridade": a builder, clinic or investor JWT and an API key are ``403``
  with no credit SQL; no credentials is ``401``; the switch off, or the gate
  closed (schema below ``046``, revision unknown, a reading that fails), is
  ``503 credit_unavailable`` before the body is read and with NO SQL; the
  reading is captured once per request;
* "idempotência pela API": the same key and request answers the same body
  with one lot; the key reused on another body, builder, lot or operation is
  ``409 idempotency_key_reused`` with nothing written;
* "retirada pela API": ``{credits: N}`` and ``{remove_all: true}``, the
  fraction of a credit, the ``409 courtesy_removal_exceeds_balance`` and
  ``404 courtesy_lot_not_found`` refusals, removal of an inactive builder's
  lot, and ``LedgerClockAhead`` as ``503 credit_clock_ahead``;
* "leitura do builder": ``/v1/credits/lots``, ``/expiries`` and
  ``/balance`` show ``kind: courtesy``, ``removed_mc`` and
  ``courtesy_available_mc`` inside ``available_mc``; the reason never;
* "cobertura e relatórios": ``GET /rollout/bindings/{builder}`` reports
  ``usable_courtesy`` and the same ``usable`` / ``reconciliation_failure``
  with and without the courtesy.

PostgreSQL (the real switch on a ``046`` and a ``045`` database, concurrent
requests of one key, the server log) is
``tests/integration/test_credit_courtesy_http_postgres.py``.  Run also under
``-p tests.credit_switch_forced_plugin``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, update

from src.api.routes import admin_credits
from src.config.settings import settings
from src.models.billing import (
    BillingCheckoutSession,
    BillingOutboxEvent,
    BillingSubscription,
    BillingSubscriptionCycle,
    BillingTopupOrder,
)
from src.models.builder import Builder
from src.models.credit import CreditCourtesyEvent, CreditLedgerEntry, CreditLot
from src.repositories.database import get_db_engine, get_db_session
from src.services import credit_courtesy
from src.services.credit_ledger import (
    CreditLedger,
    GrantSpec,
    LedgerClockAhead,
    civil_date,
    courtesy_lot_expires_at,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_rollout import CreditRolloutService
from src.services.credit_switch import (
    COURTESY_SCHEMA_MIN_REVISION,
    CreditSwitch,
    CreditSwitchReading,
    load_image_migration_chain,
)
from tests.api.test_credit_operational_authorization import ADMIN, World

pytest_plugins = ["tests.api.test_credit_operational_authorization"]

UTC = timezone.utc
GRANTS = f"{ADMIN}/courtesy-grants"
MC = 1_000
UNAVAILABLE = {"code": "credit_unavailable"}
_CHAIN = load_image_migration_chain()
_HEAD = _CHAIN[-1]
_BELOW_046 = _CHAIN[_CHAIN.index(COURTESY_SCHEMA_MIN_REVISION) - 1]


# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


@pytest.fixture
def reading(monkeypatch: pytest.MonkeyPatch) -> Callable[[CreditSwitchReading], None]:
    """Patch the switch READING (never publish one): open by default."""

    def force(value: CreditSwitchReading) -> None:
        monkeypatch.setattr(CreditSwitch, "reading", lambda _self: value)

    force(CreditSwitchReading(True, "schema_ready", _HEAD))
    return force


@pytest.fixture
def courtesy(client: Any, world: World, reading: Any) -> World:
    """The operational world (two builders, super admins) with the gate open."""

    return world


def _jwt(world: World, role: str, **claims: Any) -> dict[str, str]:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(world.tenant),
            "role": role,
            "iat": now,
            "exp": now + timedelta(hours=1),
            **claims,
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _builder_jwt(world: World) -> dict[str, str]:
    return _jwt(world, "builder", builder_id=str(world.builder))


def _admin(world: World, name: str = "admin_a") -> dict[str, str]:
    return world.bearer(name)


def _key(headers: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key or f"key-{uuid4()}"}


def _grant(
    client: Any,
    world: World,
    *,
    builder: UUID | None = None,
    credits: int = 10,
    reason: str = "conta de teste",
    key: str | None = None,
    by: str = "admin_a",
) -> Any:
    return client.post(
        GRANTS,
        json={
            "builder_id": str(builder or world.builder),
            "credits": credits,
            "reason": reason,
        },
        headers=_key(_admin(world, by), key),
    )


def _granted(client: Any, world: World, **options: Any) -> dict[str, Any]:
    response = _grant(client, world, **options)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _remove(
    client: Any,
    world: World,
    lot_id: Any,
    body: dict[str, Any],
    *,
    key: str | None = None,
) -> Any:
    return client.post(
        f"{GRANTS}/{lot_id}/removals",
        json={"reason": "retirada de teste", **body},
        headers=_key(_admin(world), key),
    )


def _read(client: Any, world: World, builder: UUID | None = None) -> dict[str, Any]:
    response = client.get(
        GRANTS,
        params={"builder_id": str(builder or world.builder)},
        headers=_admin(world),
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return dict(response.json())


def _lot(client: Any, world: World, lot_id: Any) -> dict[str, Any]:
    [lot] = [
        lot for lot in _read(client, world)["lots"] if lot["lot_id"] == str(lot_id)
    ]
    return lot


async def _count(model: Any, *where: Any) -> int:
    session = get_db_session()
    try:
        statement = select(func.count()).select_from(model)
        if where:
            statement = statement.where(*where)
        return int(await session.scalar(statement) or 0)
    finally:
        await session.close()


_WRITTEN = (CreditLot, CreditLedgerEntry, CreditCourtesyEvent)
_BILLING = (
    BillingSubscription,
    BillingSubscriptionCycle,
    BillingCheckoutSession,
    BillingTopupOrder,
    BillingOutboxEvent,
)


def _written(client: Any) -> dict[str, int]:
    """Rows of every table a courtesy write touches (lots, entries, events)."""

    return {model.__tablename__: client._submit(_count(model)) for model in _WRITTEN}


def _billing(client: Any) -> dict[str, int]:
    return {model.__tablename__: client._submit(_count(model)) for model in _BILLING}


@contextmanager
def _statements() -> Iterator[list[str]]:
    """Every SQL statement the application runs inside the block."""

    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(" ".join(statement.split()).lower())

    engine = get_db_engine().sync_engine
    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)


def _credit_sql(statements: list[str]) -> list[str]:
    return [s for s in statements if "credit_" in s or "builders" in s]


async def _set_builder(builder_id: UUID, **values: Any) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            await session.execute(
                update(Builder).where(Builder.id == builder_id).values(**values)
            )
    finally:
        await session.close()


async def _new_builder(**values: Any) -> UUID:
    builder_id = uuid4()
    session = get_db_session()
    try:
        async with session.begin():
            session.add(
                Builder(id=builder_id, name=f"B {builder_id.hex[:6]}", **values)
            )
    finally:
        await session.close()
    return builder_id


async def _paid_lot(builder_id: UUID, kind: str) -> UUID:
    """A monthly or top-up lot through the real ledger (no ASAAS rows)."""

    session = get_db_session()
    try:
        async with session.begin():
            ledger = CreditLedger(session)
            now = datetime.now(UTC)
            book = await ledger.open_book(builder_id, at=now)
            today = civil_date(now)
            if kind == "monthly":
                subscription = uuid4()
                end = today + timedelta(days=20)
                spec = GrantSpec(
                    lot_kind="monthly",
                    effect_id=monthly_grant_effect_id(subscription, today, end),
                    payment_id=f"pay-{uuid4()}",
                    paid_at=today,
                    granted_mc=5 * MC,
                    expires_at=monthly_lot_expires_at(end),
                    subscription_id=subscription,
                    cycle_start=today,
                    cycle_end=end,
                )
            else:
                order = uuid4()
                spec = GrantSpec(
                    lot_kind="topup",
                    effect_id=topup_grant_effect_id(order),
                    payment_id=f"pay-{order}",
                    paid_at=today,
                    granted_mc=5 * MC,
                    expires_at=topup_lot_expires_at(today),
                    order_id=order,
                )
            lot = book.grant(spec, at=book.clock or now)
            await ledger.persist(book, lifecycle_source=None)
            return lot.id
    finally:
        await session.close()


async def _debit(builder_id: UUID, amount_mc: int) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            await CreditLedger(session).debit(
                builder_id,
                f"trace:{uuid4()}",
                amount_mc,
                at=datetime.now(UTC),
                effect_kind="trace",
            )
    finally:
        await session.close()


async def _expired_courtesy(builder_id: UUID) -> UUID:
    """A courtesy granted 31 days ago (the ledger's clock set in the past)."""

    past = datetime.now(UTC) - timedelta(days=31)

    async def clock() -> datetime:
        return past

    session = get_db_session()
    try:
        async with session.begin():
            result = await credit_courtesy.grant(
                session,
                builder_id=builder_id,
                credits=3,
                reason="expirada",
                actor_user_id=uuid4(),
                idempotency_key=f"expired-{uuid4()}",
                at=past,
                ledger=CreditLedger(session, reference_clock=clock),
            )
            return result.lot_id
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Concessão.
# ---------------------------------------------------------------------------


def test_a_super_admin_grant_is_one_courtesy_lot_and_one_event(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    billing_before = _billing(client)
    body = _granted(client, world, credits=5_000, reason="piloto da clinica")

    occurred = datetime.fromisoformat(body["occurred_at"])
    assert body["granted_mc"] == 5_000 * MC
    assert body["builder_id"] == str(world.builder)
    assert body["actor_user_id"] == str(world.users["admin_a"])
    expires = courtesy_lot_expires_at(civil_date(occurred))
    assert datetime.fromisoformat(body["expires_at"]) == expires
    assert set(body) == {
        "event_id",
        "builder_id",
        "lot_id",
        "granted_mc",
        "expires_at",
        "occurred_at",
        "actor_user_id",
    }
    assert _written(client) == {
        "credit_lots": 1,
        "credit_ledger_entries": 1,
        "credit_courtesy_events": 1,
    }
    read = _read(client, world)
    [lot] = read["lots"]
    assert lot == {
        "lot_id": body["lot_id"],
        "kind": "courtesy",
        "status": "active",
        "granted_mc": 5_000 * MC,
        "available_mc": 5_000 * MC,
        "consumed_mc": 0,
        "removed_mc": 0,
        "expired_mc": 0,
        "paid_at": civil_date(occurred).isoformat(),
        "expires_at": lot["expires_at"],
    }
    assert datetime.fromisoformat(lot["expires_at"]) == expires
    [grant_event] = read["events"]
    assert grant_event["kind"] == "grant"
    assert grant_event["lot_id"] == body["lot_id"]
    assert grant_event["amount_mc"] == 5_000 * MC
    assert grant_event["reason"] == "piloto da clinica"
    assert grant_event["actor_user_id"] == str(world.users["admin_a"])
    # Capacity, not money: no ASAAS row of any kind.
    assert _billing(client) == billing_before


def test_the_grant_bounds_are_accepted(client: Any, courtesy: World) -> None:
    world = courtesy
    low = _granted(client, world, credits=1, reason="x")
    high = _granted(client, world, credits=100_000, reason="y" * 500)
    assert (low["granted_mc"], high["granted_mc"]) == (MC, 100_000 * MC)


# ---------------------------------------------------------------------------
# Recusas.
# ---------------------------------------------------------------------------


_BAD_GRANTS: dict[str, tuple[dict[str, Any], tuple[str, ...]]] = {
    "zero": ({"credits": 0}, ("body", "credits")),
    "negative": ({"credits": -5}, ("body", "credits")),
    "fraction": ({"credits": 1.5}, ("body", "credits")),
    "float_whole": ({"credits": 2.0}, ("body", "credits")),
    "string": ({"credits": "5"}, ("body", "credits")),
    "boolean": ({"credits": True}, ("body", "credits")),
    "above_max": ({"credits": 100_001}, ("body", "credits")),
    "empty_reason": ({"reason": ""}, ("body", "reason")),
    "long_reason": ({"reason": "x" * 501}, ("body", "reason")),
    "nul_reason": ({"reason": "a\x00b"}, ("body", "reason")),
    "blank_reason": ({"reason": "   "}, ("body", "reason")),
    "whitespace_reason": ({"reason": "\t\n "}, ("body", "reason")),
    "extra_field": ({"expires_at": "2027-01-01"}, ("body", "expires_at")),
    "actor_in_body": ({"actor_user_id": str(uuid4())}, ("body", "actor_user_id")),
    "no_builder": ({"builder_id": None}, ("body", "builder_id")),
}


@pytest.mark.parametrize("case", sorted(_BAD_GRANTS))
def test_a_malformed_grant_is_422_with_nothing_written(
    client: Any, courtesy: World, case: str
) -> None:
    world = courtesy
    change, location = _BAD_GRANTS[case]
    body: dict[str, Any] = {
        "builder_id": str(world.builder),
        "credits": 10,
        "reason": "ok",
        **change,
    }
    response = client.post(GRANTS, json=body, headers=_key(_admin(world)))
    assert response.status_code == 422, response.text
    assert [tuple(error["loc"])[:2] for error in response.json()["detail"]] == [
        location
    ]
    assert _written(client) == dict.fromkeys(
        ("credit_lots", "credit_ledger_entries", "credit_courtesy_events"), 0
    )


@pytest.mark.parametrize(
    ("key", "error_type"),
    [
        (None, "missing"),
        ("", "string_too_short"),
        ("k" * 201, "string_too_long"),
    ],
)
def test_the_idempotency_key_header_is_required_and_bounded(
    client: Any, courtesy: World, key: str | None, error_type: str
) -> None:
    world = courtesy
    headers = _admin(world)
    if key is not None:
        headers = {**headers, "Idempotency-Key": key}
    response = client.post(
        GRANTS,
        json={"builder_id": str(world.builder), "credits": 1, "reason": "x"},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert (tuple(error["loc"]), error["type"]) == (
        ("header", "Idempotency-Key"),
        error_type,
    )
    assert _written(client)["credit_courtesy_events"] == 0


def test_the_key_bounds_are_accepted(client: Any, courtesy: World) -> None:
    world = courtesy
    assert _grant(client, world, key="k").status_code == 201
    assert _grant(client, world, key="k" * 200).status_code == 201


def test_an_absent_builder_is_404_builder_not_found(
    client: Any, courtesy: World
) -> None:
    response = _grant(client, courtesy, builder=uuid4())
    assert (response.status_code, response.json()) == (
        404,
        {"code": "builder_not_found"},
    )
    assert _written(client) == dict.fromkeys(
        ("credit_lots", "credit_ledger_entries", "credit_courtesy_events"), 0
    )


@pytest.mark.parametrize(
    "values", [{"is_active": False}, {"status": "suspended"}], ids=["flag", "status"]
)
def test_an_inactive_builder_is_409_builder_inactive(
    client: Any, courtesy: World, values: dict[str, Any]
) -> None:
    inactive = client._submit(_new_builder(**values))
    response = _grant(client, courtesy, builder=inactive)
    assert (response.status_code, response.json()) == (
        409,
        {"code": "builder_inactive"},
    )
    assert _written(client) == dict.fromkeys(
        ("credit_lots", "credit_ledger_entries", "credit_courtesy_events"), 0
    )


# ---------------------------------------------------------------------------
# Autoridade, o switch e o portão 046.
# ---------------------------------------------------------------------------


def _routes(world: World) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        (
            "POST",
            GRANTS,
            {"builder_id": str(world.builder), "credits": 1, "reason": "x"},
        ),
        ("POST", f"{GRANTS}/{uuid4()}/removals", {"credits": 1, "reason": "x"}),
        ("GET", f"{GRANTS}?builder_id={world.builder}", None),
    ]


def _call(
    client: Any, method: str, path: str, body: Any, headers: dict[str, str]
) -> Any:
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, json=body, headers=_key(headers))


_CALLERS = {
    "builder": ("super_admin_jwt_required", lambda w: _builder_jwt(w)),
    "clinic_admin": ("super_admin_jwt_required", lambda w: w.bearer("officer_1")),
    "investor": ("super_admin_jwt_required", lambda w: _jwt(w, "investor")),
    "api_key": ("jwt_required", lambda w: {"X-API-Key": w.api_key}),
}


@pytest.mark.parametrize("caller", sorted(_CALLERS))
@pytest.mark.parametrize("route", [0, 1, 2], ids=["grant", "removal", "read"])
def test_only_a_super_admin_jwt_reaches_a_courtesy_route(
    client: Any, courtesy: World, caller: str, route: int
) -> None:
    world = courtesy
    code, headers_of = _CALLERS[caller]
    method, path, body = _routes(world)[route]
    with _statements() as statements:
        response = _call(client, method, path, body, headers_of(world))
    assert (response.status_code, response.json()) == (403, {"detail": {"code": code}})
    assert _credit_sql(statements) == []
    assert _written(client) == dict.fromkeys(
        ("credit_lots", "credit_ledger_entries", "credit_courtesy_events"), 0
    )


@pytest.mark.parametrize("route", [0, 1, 2], ids=["grant", "removal", "read"])
def test_no_credentials_is_401(client: Any, courtesy: World, route: int) -> None:
    method, path, body = _routes(courtesy)[route]
    response = (
        client.get(path)
        if method == "GET"
        else client.post(path, json=body, headers={"Idempotency-Key": "k"})
    )
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


@pytest.mark.parametrize("route", [0, 1, 2], ids=["grant", "removal", "read"])
def test_the_switch_off_is_503_with_no_sql(
    client: Any, courtesy: World, switch: Callable[[bool], None], route: int
) -> None:
    world = courtesy
    switch(False)
    method, path, body = _routes(world)[route]
    with _statements() as statements:
        response = _call(client, method, path, body, _admin(world))
    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert statements == []


_CLOSED_READINGS = {
    "schema_045": CreditSwitchReading(True, "schema_ready", _BELOW_046),
    "revision_absent": CreditSwitchReading(True, "schema_ready", None),
    "reading_off": CreditSwitchReading(False, "read_error", _HEAD),
    "revision_unknown": CreditSwitchReading(True, "schema_ready", "not-a-revision"),
}


@pytest.mark.parametrize("closed", sorted(_CLOSED_READINGS))
@pytest.mark.parametrize("route", [0, 1, 2], ids=["grant", "removal", "read"])
def test_a_closed_courtesy_gate_is_503_before_the_body_and_with_no_sql(
    client: Any, courtesy: World, reading: Any, closed: str, route: int
) -> None:
    """The switch stays effective (every other credit surface answers); only
    the courtesy routes are closed, whoever asks and whatever the body."""

    world = courtesy
    reading(_CLOSED_READINGS[closed])
    method, path, body = _routes(world)[route]
    with _statements() as statements:
        response = _call(client, method, path, body, _admin(world))
        malformed = client.request(
            method,
            path,
            content=b"{not json",
            headers=_key({**_admin(world), "Content-Type": "application/json"}),
        )
        builder = _call(client, method, path, body, _builder_jwt(world))
    for answer in (response, malformed, builder):
        assert (answer.status_code, answer.json()) == (503, UNAVAILABLE)
    assert statements == []
    # The other credit surfaces keep answering on the same reading.
    rollout = client.get(
        f"{ADMIN}/rollout/bindings/{world.builder}", headers=_admin(world)
    )
    assert rollout.status_code == 200, rollout.text


@pytest.mark.parametrize("failing", ["credit_switch_reading", "courtesy_gate_open"])
def test_a_gate_that_raises_is_closed_never_a_500(
    client: Any,
    courtesy: World,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
) -> None:
    def boom(*_args: Any) -> Any:
        raise RuntimeError("unreadable")

    monkeypatch.setattr(admin_credits, failing, boom)
    method, path, body = _routes(courtesy)[0]
    with _statements() as statements:
        response = _call(client, method, path, body, _admin(courtesy))
    assert (response.status_code, response.json()) == (503, UNAVAILABLE)
    assert statements == []


@pytest.mark.parametrize("route", [0, 1, 2], ids=["grant", "removal", "read"])
def test_the_gate_captures_the_switch_reading_once_per_request(
    client: Any,
    courtesy: World,
    monkeypatch: pytest.MonkeyPatch,
    route: int,
) -> None:
    calls: list[CreditSwitchReading] = []
    original = admin_credits.credit_switch_reading

    def counted() -> CreditSwitchReading:
        value = original()
        calls.append(value)
        return value

    monkeypatch.setattr(admin_credits, "credit_switch_reading", counted)
    method, path, body = _routes(courtesy)[route]
    response = _call(client, method, path, body, _admin(courtesy))
    assert response.status_code in (200, 201, 404), response.text
    assert calls == [CreditSwitchReading(True, "schema_ready", _HEAD)]


# ---------------------------------------------------------------------------
# Idempotência pela API.
# ---------------------------------------------------------------------------


def test_the_same_key_and_request_is_the_same_answer_and_one_lot(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    first = _grant(client, world, credits=7, key="same-key")
    second = _grant(client, world, credits=7, key="same-key")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert _written(client) == {
        "credit_lots": 1,
        "credit_ledger_entries": 1,
        "credit_courtesy_events": 1,
    }
    lot = first.json()["lot_id"]
    removal = _remove(client, world, lot, {"credits": 2}, key="removal-key")
    again = _remove(client, world, lot, {"credits": 2}, key="removal-key")
    assert removal.status_code == again.status_code == 201
    assert removal.json() == again.json()
    assert _lot(client, world, lot)["removed_mc"] == 2 * MC


def test_a_reused_key_on_another_request_is_409_with_nothing_written(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    lot = _granted(client, world, credits=10, key="grant-key")["lot_id"]
    other_lot = _granted(client, world, credits=10, key="other-grant")["lot_id"]
    assert _remove(client, world, lot, {"credits": 1}, key="removal-key").status_code
    before = _written(client)
    reused = {
        "other_body": _grant(client, world, credits=11, key="grant-key"),
        "other_reason": _grant(client, world, credits=10, reason="z", key="grant-key"),
        "other_builder": _grant(
            client, world, builder=world.other_builder, credits=10, key="grant-key"
        ),
        "grant_key_on_removal": _remove(
            client, world, lot, {"credits": 1}, key="grant-key"
        ),
        "removal_key_on_another_lot": _remove(
            client, world, other_lot, {"credits": 1}, key="removal-key"
        ),
        "removal_key_other_body": _remove(
            client, world, lot, {"remove_all": True}, key="removal-key"
        ),
        "removal_key_on_a_grant": _grant(client, world, key="removal-key"),
    }
    for case, response in reused.items():
        assert (response.status_code, response.json()) == (
            409,
            {"code": "idempotency_key_reused"},
        ), case
    assert _written(client) == before
    # The same key by ANOTHER super admin, same request: the recorded answer.
    replay = _grant(client, world, credits=10, key="grant-key", by="admin_b")
    assert replay.status_code == 201
    assert replay.json()["actor_user_id"] == str(world.users["admin_a"])
    assert _written(client) == before


# ---------------------------------------------------------------------------
# Retirada pela API.
# ---------------------------------------------------------------------------


def test_a_removal_of_n_credits_is_funding_removed_not_consumption(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    lot = _granted(client, world, credits=10_000)["lot_id"]
    response = _remove(client, world, lot, {"credits": 6_000})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["removed_mc"] == 6_000 * MC
    assert body["lot_id"] == lot and body["builder_id"] == str(world.builder)
    assert set(body) == {
        "event_id",
        "builder_id",
        "lot_id",
        "removed_mc",
        "occurred_at",
        "actor_user_id",
    }
    after = _lot(client, world, lot)
    assert (after["available_mc"], after["removed_mc"], after["consumed_mc"]) == (
        4_000 * MC,
        6_000 * MC,
        0,
    )
    events = _read(client, world)["events"]
    assert [(e["kind"], e["amount_mc"]) for e in events] == [
        ("grant", 10_000 * MC),
        ("removal", 6_000 * MC),
    ]
    assert events[1]["reason"] == "retirada de teste"
    # Capacity measures consumption only: nothing used, no threshold.
    thresholds = client.get("/v1/credits/thresholds", headers=_builder_jwt(world))
    assert thresholds.status_code == 200, thresholds.text
    assert (thresholds.json()["used_mc"], thresholds.json()["used_percent"]) == (0, 0)
    assert thresholds.json()["reached"] == []


def test_the_fraction_of_a_credit_and_remove_all(client: Any, courtesy: World) -> None:
    world = courtesy
    lot = _granted(client, world, credits=5_000)["lot_id"]
    client._submit(_debit(world.builder, 600))
    assert _lot(client, world, lot)["available_mc"] == 4_999_400
    over = _remove(client, world, lot, {"credits": 5_000})
    assert (over.status_code, over.json()) == (
        409,
        {"code": "courtesy_removal_exceeds_balance"},
    )
    partial = _remove(client, world, lot, {"credits": 4_999})
    assert partial.status_code == 201, partial.text
    assert _lot(client, world, lot)["available_mc"] == 400
    rest = _remove(client, world, lot, {"remove_all": True})
    assert rest.status_code == 201, rest.text
    assert rest.json()["removed_mc"] == 400
    emptied = _lot(client, world, lot)
    assert (emptied["status"], emptied["available_mc"], emptied["removed_mc"]) == (
        "exhausted",
        0,
        4_999_400,
    )
    assert emptied["consumed_mc"] == 600
    before = _written(client)
    again = _remove(client, world, lot, {"remove_all": True})
    one = _remove(client, world, lot, {"credits": 1})
    for response in (again, one):
        assert (response.status_code, response.json()) == (
            409,
            {"code": "courtesy_removal_exceeds_balance"},
        )
    assert _written(client) == before


def test_remove_all_of_an_expired_lot_is_409(client: Any, courtesy: World) -> None:
    world = courtesy
    lot = client._submit(_expired_courtesy(world.builder))
    assert _lot(client, world, lot)["status"] == "expired"
    before = _written(client)
    response = _remove(client, world, lot, {"remove_all": True})
    assert (response.status_code, response.json()) == (
        409,
        {"code": "courtesy_removal_exceeds_balance"},
    )
    assert _written(client) == before


@pytest.mark.parametrize("kind", ["monthly", "topup", "absent"])
def test_only_a_courtesy_lot_can_be_removed_404_otherwise(
    client: Any, courtesy: World, kind: str
) -> None:
    world = courtesy
    lot = (
        uuid4() if kind == "absent" else client._submit(_paid_lot(world.builder, kind))
    )
    before = _written(client)
    for body in ({"credits": 1}, {"remove_all": True}):
        response = _remove(client, world, lot, body)
        assert (response.status_code, response.json()) == (
            404,
            {"code": "courtesy_lot_not_found"},
        )
    assert _written(client) == before


def test_an_inactive_builders_courtesy_can_still_be_removed(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    lot = _granted(client, world, credits=3)["lot_id"]
    client._submit(_set_builder(world.builder, is_active=False, status="inactive"))
    response = _remove(client, world, lot, {"remove_all": True})
    assert response.status_code == 201, response.text
    assert response.json()["removed_mc"] == 3 * MC
    refused = _grant(client, world, credits=3)
    assert (refused.status_code, refused.json()) == (409, {"code": "builder_inactive"})


_BAD_REMOVALS: dict[str, dict[str, Any]] = {
    "neither": {},
    "both": {"credits": 1, "remove_all": True},
    "remove_all_false": {"remove_all": False},
    "remove_all_false_with_credits": {"credits": 1, "remove_all": False},
    "zero": {"credits": 0},
    "string": {"credits": "1"},
    "fraction": {"credits": 0.5},
    "remove_all_string": {"remove_all": "true"},
    "extra": {"credits": 1, "builder_id": str(uuid4())},
    "nul_reason": {"credits": 1, "reason": "\x00"},
    "empty_reason": {"credits": 1, "reason": ""},
    "long_reason": {"credits": 1, "reason": "r" * 501},
    "blank_reason": {"credits": 1, "reason": "   "},
    "whitespace_reason": {"remove_all": True, "reason": "\t\n "},
    "above_max": {"credits": 100_001},
    "huge": {"credits": 10**30},
}


@pytest.mark.parametrize("case", sorted(_BAD_REMOVALS))
def test_a_malformed_removal_is_422_with_nothing_written(
    client: Any, courtesy: World, case: str
) -> None:
    world = courtesy
    lot = _granted(client, world, credits=3)["lot_id"]
    before = _written(client)
    response = _remove(client, world, lot, _BAD_REMOVALS[case])
    assert response.status_code == 422, response.text
    assert all(error["loc"][0] == "body" for error in response.json()["detail"])
    assert _written(client) == before


@pytest.mark.parametrize("operation", ["grant", "removal"])
def test_a_ledger_clock_ahead_is_503_credit_clock_ahead(
    client: Any, courtesy: World, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    world = courtesy
    lot = _granted(client, world, credits=3)["lot_id"]
    before = _written(client)

    async def ahead(*_args: Any, **_kwargs: Any) -> Any:
        raise LedgerClockAhead("the writer's clock is ahead")

    monkeypatch.setattr(
        credit_courtesy, "grant" if operation == "grant" else "remove", ahead
    )
    response = (
        _grant(client, world)
        if operation == "grant"
        else _remove(client, world, lot, {"credits": 1})
    )
    assert (response.status_code, response.json()) == (
        503,
        {"code": "credit_clock_ahead"},
    )
    assert _written(client) == before


# ---------------------------------------------------------------------------
# GET /courtesy-grants.
# ---------------------------------------------------------------------------


def test_the_read_lists_only_this_builders_courtesy(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    mine = _granted(client, world, credits=2)["lot_id"]
    _granted(client, world, builder=world.other_builder, credits=4)
    client._submit(_paid_lot(world.builder, "topup"))
    read = _read(client, world)
    assert [lot["lot_id"] for lot in read["lots"]] == [mine]
    assert [event["lot_id"] for event in read["events"]] == [mine]
    assert set(read) == {"as_of", "lots", "events"}


def test_the_read_of_an_absent_builder_is_404(client: Any, courtesy: World) -> None:
    response = client.get(
        GRANTS, params={"builder_id": str(uuid4())}, headers=_admin(courtesy)
    )
    assert (response.status_code, response.json()) == (
        404,
        {"code": "builder_not_found"},
    )
    assert response.headers["Cache-Control"] == "no-store"
    missing = client.get(GRANTS, headers=_admin(courtesy))
    assert missing.status_code == 422
    assert [tuple(e["loc"]) for e in missing.json()["detail"]] == [
        ("query", "builder_id")
    ]


def test_the_read_of_a_builder_without_courtesy_is_empty(
    client: Any, courtesy: World
) -> None:
    read = _read(client, courtesy)
    assert (read["lots"], read["events"]) == ([], [])


# ---------------------------------------------------------------------------
# Leitura do builder.
# ---------------------------------------------------------------------------


def test_the_builder_reads_its_courtesy_never_the_reason(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    lot = _granted(client, world, credits=8, reason="motivo secreto")["lot_id"]
    client._submit(_paid_lot(world.builder, "topup"))
    assert _remove(client, world, lot, {"credits": 3}).status_code == 201
    headers = _builder_jwt(world)

    lots = client.get("/v1/credits/lots", headers=headers)
    expiries = client.get("/v1/credits/expiries", headers=headers)
    balance = client.get("/v1/credits/balance", headers=headers)
    summary = client.get("/v1/credits/summary", headers=headers)
    for response in (lots, expiries, balance, summary):
        assert response.status_code == 200, response.text
        assert "motivo secreto" not in response.text
    by_kind = {item["kind"]: item for item in lots.json()["lots"]}
    assert set(by_kind) == {"courtesy", "topup"}
    assert (
        by_kind["courtesy"]["available_mc"],
        by_kind["courtesy"]["removed_mc"],
        by_kind["courtesy"]["status"],
    ) == (5 * MC, 3 * MC, "active")
    assert by_kind["topup"]["removed_mc"] == 0
    lines = {item["kind"]: item for item in expiries.json()["expiries"]}
    assert (lines["courtesy"]["removed_mc"], lines["topup"]["removed_mc"]) == (
        3 * MC,
        0,
    )
    figures = balance.json()
    assert figures["courtesy_available_mc"] == 5 * MC
    assert figures["available_mc"] == (
        figures["monthly_available_mc"]
        + figures["topup_available_mc"]
        + figures["courtesy_available_mc"]
    )
    # The courtesy expires first (30 days < 6 months): the next expiry.
    assert figures["next_expiry"] == {
        "lot_id": lot,
        "kind": "courtesy",
        "expires_at": figures["next_expiry"]["expires_at"],
        "available_mc": 5 * MC,
        "removed_mc": 3 * MC,
    }
    assert summary.json()["balance"] == figures


# ---------------------------------------------------------------------------
# Cobertura e relatórios.
# ---------------------------------------------------------------------------


def test_the_binding_status_reports_usable_courtesy_only_for_coverage(
    client: Any, courtesy: World
) -> None:
    world = courtesy
    path = f"{ADMIN}/rollout/bindings/{world.builder}"
    without = client.get(path, headers=_admin(world))
    assert without.status_code == 200, without.text
    lot = _granted(client, world, credits=2)["lot_id"]
    with_courtesy = client.get(path, headers=_admin(world))
    assert with_courtesy.status_code == 200, with_courtesy.text
    assert (
        without.json()["usable_courtesy"],
        with_courtesy.json()["usable_courtesy"],
    ) == (
        False,
        True,
    )
    for field in (
        "usable",
        "reconciliation_failure",
        "monthly_binding",
        "usable_topup",
    ):
        assert without.json()[field] == with_courtesy.json()[field], field
    assert _remove(client, world, lot, {"remove_all": True}).status_code == 201
    emptied = client.get(path, headers=_admin(world))
    assert emptied.json()["usable_courtesy"] is False
    assert emptied.json()["usable"] == without.json()["usable"]


async def _coverage(builder_id: UUID, *, include_courtesy: bool) -> Any:
    session = get_db_session()
    try:
        return await CreditRolloutService(session).binding_coverage(
            builder_id, include_courtesy=include_courtesy
        )
    finally:
        await session.rollback()
        await session.close()


def test_the_enforced_coverage_reads_no_courtesy_statement(
    client: Any, courtesy: World
) -> None:
    """The enforced callers read ``usable`` alone: by default
    ``binding_coverage`` runs exactly the statements it ran before the
    courtesy (no courtesy read); ``include_courtesy=True`` (the cutoff report
    and the binding status) runs those same statements plus exactly one."""

    world = courtesy
    _granted(client, world, credits=2)
    with _statements() as default_sql:
        default = client._submit(_coverage(world.builder, include_courtesy=False))
    with _statements() as courtesy_sql:
        full = client._submit(_coverage(world.builder, include_courtesy=True))
    assert len(default_sql) == len(courtesy_sql) - 1
    assert default_sql == courtesy_sql[:-1]
    assert "credit_lots" in courtesy_sql[-1]
    assert "billing_" not in courtesy_sql[-1]
    assert (default.usable_courtesy, full.usable_courtesy) == (False, True)
    assert (default.usable, default.reconciliation_failure) == (
        full.usable,
        full.reconciliation_failure,
    )
    assert default.covered == (
        not default.quarantined
        and not default.identity_conflict
        and (default.monthly_binding or default.usable_topup)
    )
    assert full.covered


def test_the_routes_follow_the_forced_plugin_without_a_patched_reading(
    client: Any, world: World, request: pytest.FixtureRequest
) -> None:
    """No ``reading`` fixture here: under ``-p
    tests.credit_switch_forced_plugin`` the plugin's forced reading (head of
    the chain) opens the gate and the grant is written; without it SQLite
    never publishes a reading, so the gate stays closed (503, no SQL)."""

    forced = request.config.pluginmanager.has_plugin(
        "tests.credit_switch_forced_plugin"
    )
    with _statements() as statements:
        response = _grant(client, world, credits=1)
    if forced:
        assert response.status_code == 201, response.text
        assert _written(client)["credit_courtesy_events"] == 1
    else:
        assert (response.status_code, response.json()) == (503, UNAVAILABLE)
        assert statements == []
