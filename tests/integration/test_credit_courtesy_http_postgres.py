"""Courtesy credits over HTTP on real PostgreSQL (creditos-cortesia Group 3).

The real application (``src.api.main.app``: lifespan, middleware, the
process pool) on a disposable PostgreSQL database, with
``credit_hot_path_enabled`` on and the REAL credit switch reading -- the
``alembic_version`` the lifespan reads, never a forced or published one:

* at head (``046``) the courtesy gate opens: a super admin's grant is one
  courtesy lot with no ``billing_*`` row; the removal, the admin read, the
  builder's ``/v1/credits/*`` read, the binding coverage and
  ``/builders/subscriptions`` answer on PostgreSQL's real types; the
  ``/builders/subscriptions`` statements never name
  ``credit_courtesy_events`` and its courtesy part is one statement;
* the production shape of 2026-11-10: a builder user whose home tenant is
  linked to no builder logs in and reads its own account and courtesy lot
  through ``/v1/credits/summary`` and ``/lots`` (no ``tenant_id``) and
  exports that tenant's bytes; once its ``users`` row goes stale
  (deactivated, deleted, demoted, moved to another builder or tenant) the
  same token gets the absent tenant's 403 with no ``credit_*`` or
  ``billing_*`` statement -- the ``users`` re-read on PostgreSQL's real
  types (``users.role`` as Alembic built it); an unlinked tenant shared by
  builder users of two builders is refused to both (credit reads and
  export), while a second user of the same builder and a ``clinic_admin``
  user leave the fallback in place;
* concurrent requests of one ``Idempotency-Key``: the same request is one
  lot and the same answer; the key on two builders is one lot and
  ``409 idempotency_key_reused`` for the loser -- never a 500;
* at ``045`` (the image of PR-B over the database before the window) the
  switch stays effective and every other credit surface answers, but the
  three courtesy routes are ``503 credit_unavailable`` with NO SQL and
  ``/builders/subscriptions`` answers ``courtesy: null``;
* last: the server log of these databases holds no ``ERROR`` line at all
  (no refusal here is a database error: a green negative proof that hid a
  parse or type error would show there).

Needs ``POSTGRES_SMOKE_DATABASE_URL`` (a superuser, for
``pg_current_logfile()``) and a server with ``logging_collector=on`` and
``%d`` in ``log_line_prefix``; without them the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import os
import re
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.models.builder import Builder
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

MC = 1_000
GRANTS = "/v1/admin/credits/courtesy-grants"
UNAVAILABLE = {"code": "credit_unavailable"}
_ERROR_LINE = re.compile(r"\bERROR:\s+(?P<message>.*)$")


@dataclass(frozen=True)
class _Databases:
    cluster: _Cluster
    head: URL
    at_045: URL
    names: tuple[str, ...]


@pytest.fixture(scope="module")
async def databases() -> AsyncIterator[_Databases]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    try:
        head = await cluster.fresh()
        await _migrate(cluster.url(head), "upgrade", "head")
        before = await cluster.fresh()
        await _migrate(cluster.url(before), "upgrade", "045")
        for name, revision in ((head, "046"), (before, "045")):
            assert (
                await _scalar(
                    cluster.url(name), "SELECT version_num FROM alembic_version"
                )
                == revision
            )
        yield _Databases(
            cluster=cluster,
            head=cluster.url(head),
            at_045=cluster.url(before),
            names=(head, before),
        )
    finally:
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


async def _scalar(url: URL, statement: str, **params: Any) -> Any:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text(statement), params)).scalar()
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class _World:
    builder: UUID
    other_builder: UUID
    tenant: UUID
    admin_user: UUID


async def _seed(url: URL) -> _World:
    from src.api.middleware.auth import pwd_context

    world = _World(
        builder=uuid4(), other_builder=uuid4(), tenant=uuid4(), admin_user=uuid4()
    )
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session, session.begin():
            for builder in (world.builder, world.other_builder):
                session.add(Builder(id=builder, name=f"Courtesy {builder.hex[:8]}"))
            await session.flush()
            session.add(
                Tenant(
                    id=world.tenant,
                    name=f"Courtesy tenant {world.tenant.hex[:8]}",
                    slug=f"courtesy-{world.tenant.hex}",
                    api_key_hash=pwd_context.hash(uuid4().hex),
                    builder_id=world.builder,
                    config={},
                )
            )
    finally:
        await engine.dispose()
    return world


def _bearer(world: _World, role: str, **claims: Any) -> dict[str, str]:
    from src.config.settings import settings

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(world.admin_user if role == "super_admin" else uuid4()),
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


def _admin(world: _World, key: str | None = None) -> dict[str, str]:
    headers = _bearer(world, "super_admin")
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _builder(world: _World) -> dict[str, str]:
    return _bearer(world, "builder", builder_id=str(world.builder))


@asynccontextmanager
async def _running_app(url: URL, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """The real app on ``url``; the lifespan reads the real switch."""

    import httpx

    import src.repositories as repositories
    from src.api.main import app
    from src.config.settings import settings
    from tests.conftest import _production_init_database

    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    monkeypatch.setattr(settings, "credit_switch_refresh_seconds", 3600)
    monkeypatch.setattr(
        repositories,
        "init_database",
        functools.partial(
            _production_init_database,
            database_url=url.render_as_string(hide_password=False),
            engine_factory=create_async_engine,
        ),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("testclient", 50000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client


# The response-outbox publisher's background tick reads
# ``credit_enforcement_at`` while the switch is effective (Group 3b-2; the
# dominance oracle allows exactly this statement too): it is not a statement
# of the request under test.
_BACKGROUND_ENFORCEMENT_READ = (
    "select credit_enforcement_state.credit_enforcement_at from "
    "credit_enforcement_state where credit_enforcement_state.id = $1::smallint"
)


def _of_requests(statements: list[str]) -> list[str]:
    return [s for s in statements if s != _BACKGROUND_ENFORCEMENT_READ]


@contextmanager
def _statements() -> Iterator[list[str]]:
    """Every statement the app's pool runs inside the block."""

    from src.repositories.database import get_db_engine

    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(" ".join(statement.split()).lower())

    engine = get_db_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def _billing_counts(url: URL) -> dict[str, int]:
    """Rows of EVERY ``billing_*`` table."""

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            tables = (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name LIKE 'billing\\_%' ORDER BY table_name"
                    )
                )
            ).scalars()
            names = list(tables)
            assert len(names) >= 6, names
            return {
                name: int(
                    (
                        await connection.execute(text(f'SELECT count(*) FROM "{name}"'))
                    ).scalar_one()
                )
                for name in names
            }
    finally:
        await engine.dispose()


async def _courtesy_rows(url: URL, builder: UUID) -> tuple[int, int, int]:
    """(courtesy lots, their ledger entries, events) of ``builder``."""

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM credit_lots WHERE builder_id = :b "
                        " AND lot_kind = 'courtesy'), "
                        "(SELECT count(*) FROM credit_ledger_entries e JOIN "
                        " credit_lots l ON l.id = e.lot_id WHERE e.builder_id = :b "
                        " AND l.lot_kind = 'courtesy'), "
                        "(SELECT count(*) FROM credit_courtesy_events "
                        " WHERE builder_id = :b)"
                    ),
                    {"b": builder},
                )
            ).one()
            return int(row[0]), int(row[1]), int(row[2])
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Head (046): the gate opens on the real reading.
# ---------------------------------------------------------------------------


async def test_at_046_a_super_admin_grants_removes_and_reads_over_http(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _seed(databases.head)
    billing_before = await _billing_counts(databases.head)
    async with _running_app(databases.head, monkeypatch) as client:
        from src.services.credit_switch import courtesy_gate_open, credit_switch_reading

        reading = credit_switch_reading()
        assert (reading.effective, reading.revision) == (True, "046")
        assert courtesy_gate_open(reading) is True

        grant = await client.post(
            GRANTS,
            json={"builder_id": str(world.builder), "credits": 5_000, "reason": "pg"},
            headers=_admin(world, "pg-grant"),
        )
        assert grant.status_code == 201, grant.text
        lot = grant.json()["lot_id"]
        assert grant.json()["granted_mc"] == 5_000 * MC
        assert grant.json()["actor_user_id"] == str(world.admin_user)

        removal = await client.post(
            f"{GRANTS}/{lot}/removals",
            json={"credits": 1_500, "reason": "pg"},
            headers=_admin(world, "pg-removal"),
        )
        assert removal.status_code == 201, removal.text
        exceeds = await client.post(
            f"{GRANTS}/{lot}/removals",
            json={"credits": 3_501, "reason": "pg"},
            headers=_admin(world, "pg-removal-exceeds"),
        )
        assert (exceeds.status_code, exceeds.json()) == (
            409,
            {"code": "courtesy_removal_exceeds_balance"},
        )
        absent = await client.post(
            f"{GRANTS}/{uuid4()}/removals",
            json={"remove_all": True, "reason": "pg"},
            headers=_admin(world, "pg-removal-absent"),
        )
        assert (absent.status_code, absent.json()) == (
            404,
            {"code": "courtesy_lot_not_found"},
        )
        nobody = await client.post(
            GRANTS,
            json={"builder_id": str(uuid4()), "credits": 1, "reason": "pg"},
            headers=_admin(world, "pg-grant-nobody"),
        )
        assert (nobody.status_code, nobody.json()) == (
            404,
            {"code": "builder_not_found"},
        )
        nul = await client.post(
            GRANTS,
            json={"builder_id": str(world.builder), "credits": 1, "reason": "\x00"},
            headers=_admin(world, "pg-grant-nul"),
        )
        assert nul.status_code == 422
        assert [e["loc"] for e in nul.json()["detail"]] == [["body", "reason"]]

        read = await client.get(
            GRANTS, params={"builder_id": str(world.builder)}, headers=_admin(world)
        )
        assert read.status_code == 200, read.text
        [item] = read.json()["lots"]
        assert (
            item["kind"],
            item["status"],
            item["available_mc"],
            item["removed_mc"],
            item["consumed_mc"],
        ) == ("courtesy", "active", 3_500 * MC, 1_500 * MC, 0)
        assert [(e["kind"], e["amount_mc"]) for e in read.json()["events"]] == [
            ("grant", 5_000 * MC),
            ("removal", 1_500 * MC),
        ]

        balance = await client.get("/v1/credits/balance", headers=_builder(world))
        assert balance.status_code == 200, balance.text
        assert balance.json()["courtesy_available_mc"] == 3_500 * MC
        assert balance.json()["available_mc"] == 3_500 * MC
        assert balance.json()["next_expiry"]["removed_mc"] == 1_500 * MC
        assert "pg" not in {
            str(value) for value in balance.json().values()
        }, "the reason never reaches the builder"
        lots = await client.get("/v1/credits/lots", headers=_builder(world))
        assert [(x["kind"], x["removed_mc"]) for x in lots.json()["lots"]] == [
            ("courtesy", 1_500 * MC)
        ]

        binding = await client.get(
            f"/v1/admin/credits/rollout/bindings/{world.builder}",
            headers=_admin(world),
        )
        assert binding.status_code == 200, binding.text
        assert binding.json()["usable_courtesy"] is True
        assert binding.json()["usable"] is True
        assert binding.json()["reconciliation_failure"] is False

        with _statements() as statements:
            subscriptions = await client.get(
                "/v1/admin/builders/subscriptions", headers=_admin(world)
            )
        assert subscriptions.status_code == 200, subscriptions.text
        rows = {row["id"]: row for row in subscriptions.json()}
        mine = rows[str(world.builder)]
        assert mine["courtesy"]["available_mc"] == 3_500 * MC
        assert datetime.fromisoformat(
            mine["courtesy"]["active_expires_at"]
        ) == datetime.fromisoformat(item["expires_at"])
        assert (mine["subscription"], mine["subscription_unavailable"]) == (
            None,
            False,
        )
        assert rows[str(world.other_builder)]["courtesy"] == {
            "active_expires_at": None,
            "available_mc": 0,
        }
        assert [s for s in statements if "credit_courtesy_events" in s] == []
        assert len([s for s in statements if "credit_lots" in s]) == 1
    assert await _courtesy_rows(databases.head, world.builder) == (1, 2, 2)
    assert await _billing_counts(databases.head) == billing_before


async def test_concurrent_requests_of_one_key_make_one_lot_and_no_500(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _seed(databases.head)
    async with _running_app(databases.head, monkeypatch) as client:
        body = {"builder_id": str(world.builder), "credits": 3, "reason": "race"}
        same = await asyncio.gather(
            *(
                client.post(GRANTS, json=body, headers=_admin(world, "race-same"))
                for _ in range(4)
            )
        )
        assert [r.status_code for r in same] == [201] * 4, [r.text for r in same]
        assert len({r.text for r in same}) == 1
        assert await _courtesy_rows(databases.head, world.builder) == (1, 1, 1)

        bodies = [
            {"builder_id": str(builder), "credits": 2, "reason": "race"}
            for builder in (world.builder, world.other_builder) * 2
        ]
        crossed = await asyncio.gather(
            *(
                client.post(GRANTS, json=b, headers=_admin(world, "race-crossed"))
                for b in bodies
            )
        )
    statuses = sorted(r.status_code for r in crossed)
    assert 500 not in statuses, [r.text for r in crossed]
    winners = [r for r in crossed if r.status_code == 201]
    losers = [r for r in crossed if r.status_code != 201]
    assert winners and len({r.json()["builder_id"] for r in winners}) == 1
    assert all(
        (r.status_code, r.json()) == (409, {"code": "idempotency_key_reused"})
        for r in losers
    ), [r.text for r in losers]
    winner = UUID(winners[0].json()["builder_id"])
    loser = world.other_builder if winner == world.builder else world.builder
    expected_lots = 2 if winner == world.builder else 1
    assert (await _courtesy_rows(databases.head, winner))[0] == expected_lots
    assert (await _courtesy_rows(databases.head, loser))[0] == (
        1 if loser == world.builder else 0
    )


_REPORTS = (
    "/v1/admin/credits/rollout/reconciliation",
    "/v1/admin/credits/rollout/readiness",
    "/v1/admin/credits/rollout/status",
)


async def _reports(client: Any, world: _World) -> dict[str, Any]:
    """The three rollout reports.  None carries a per-call timestamp
    (``enforcement_at`` is the stored arming instant), so nothing is masked."""

    bodies: dict[str, Any] = {}
    for path in _REPORTS:
        response = await client.get(path, headers=_admin(world))
        assert response.status_code == 200, (path, response.text)
        bodies[path] = response.json()
    return bodies


async def _cutoff_reasons(url: URL, tenant: UUID) -> set[str]:
    from src.services.credit_rollout import CreditRolloutService

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session:
            report = await CreditRolloutService(session).cutoff_report()
            await session.rollback()
    finally:
        await engine.dispose()
    return {f.reason for f in report.findings if f.tenant_id == tenant}


async def test_at_046_a_courtesy_changes_no_report_and_covers_the_cutoff(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    """creditos-cortesia "cobertura e relatórios": the reconciliation,
    readiness and status reports are identical before the grant, after it and
    after ``remove_all``; the cutoff report of a builder whose ONLY capacity
    is an active courtesy has no ``builder_without_coverage`` finding while
    it is active, and has it again once the courtesy is removed."""

    world = await _seed(databases.head)
    # Traffic this month: one ``message.in`` of the builder's tenant.
    engine = create_async_engine(databases.head, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO events (id, tenant_id, event_type, event_data, "
                    "created_at) VALUES (:id, :tenant, 'message.in', "
                    "'{}'::jsonb, now())"
                ),
                {"id": uuid4(), "tenant": world.tenant},
            )
    finally:
        await engine.dispose()
    async with _running_app(databases.head, monkeypatch) as client:
        before = await _reports(client, world)
        assert "builder_without_coverage" in await _cutoff_reasons(
            databases.head, world.tenant
        )

        grant = await client.post(
            GRANTS,
            json={"builder_id": str(world.builder), "credits": 10, "reason": "pg"},
            headers=_admin(world, "pg-reports-grant"),
        )
        assert grant.status_code == 201, grant.text
        assert await _reports(client, world) == before
        assert "builder_without_coverage" not in await _cutoff_reasons(
            databases.head, world.tenant
        )

        removal = await client.post(
            f"{GRANTS}/{grant.json()['lot_id']}/removals",
            json={"remove_all": True, "reason": "pg"},
            headers=_admin(world, "pg-reports-removal"),
        )
        assert removal.status_code == 201, removal.text
        assert await _reports(client, world) == before
        assert "builder_without_coverage" in await _cutoff_reasons(
            databases.head, world.tenant
        )


async def test_a_failed_read_never_poisons_builders_subscriptions(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database error in one builder's subscription read (or in the
    courtesy read) is rolled back to its savepoint: the other rows are still
    read on PostgreSQL, where an aborted transaction would refuse every later
    statement.  A database of its own, so the deliberate errors stay out of
    the server-log audit of the shared ones."""

    from src.api.routes import admin

    name = await databases.cluster.fresh()
    url = databases.cluster.url(name)
    await _migrate(url, "upgrade", "head")
    world = await _seed(url)
    real_read = admin.read_current_subscription

    async def read(session: Any, *, builder_id: UUID) -> Any:
        if builder_id == world.other_builder:
            await session.execute(text("SELECT * FROM no_such_table"))
        return await real_read(session, builder_id=builder_id)

    monkeypatch.setattr(admin, "read_current_subscription", read)
    async with _running_app(url, monkeypatch) as client:
        grant = await client.post(
            GRANTS,
            json={"builder_id": str(world.builder), "credits": 2, "reason": "pg"},
            headers=_admin(world, "pg-poison-grant"),
        )
        assert grant.status_code == 201, grant.text
        response = await client.get(
            "/v1/admin/builders/subscriptions", headers=_admin(world)
        )
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()}
        broken = rows[str(world.other_builder)]
        fine = rows[str(world.builder)]
        assert (broken["subscription"], broken["subscription_unavailable"]) == (
            None,
            True,
        )
        assert (fine["subscription"], fine["subscription_unavailable"]) == (
            None,
            False,
        )
        assert fine["courtesy"]["available_mc"] == 2 * MC

        async def broken_courtesy(session: Any, _at: datetime) -> Any:
            await session.execute(text("SELECT * FROM no_such_table"))

        monkeypatch.setattr(admin, "read_current_subscription", real_read)
        monkeypatch.setattr(admin, "_courtesy_by_builder", broken_courtesy)
        response = await client.get(
            "/v1/admin/builders/subscriptions", headers=_admin(world)
        )
        assert response.status_code == 200, response.text
        for row in response.json():
            assert row["courtesy"] is None
            assert (row["subscription"], row["subscription_unavailable"]) == (
                None,
                False,
            )


async def test_at_046_a_builder_reads_its_courtesy_through_an_unlinked_home_tenant(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production shape of 2026-11-10: the builder user's home tenant
    has ``tenants.builder_id`` NULL, the ``users`` row (role ``builder``)
    carries the builder.  Logged in through the real ``/v1/auth/login``,
    with no ``tenant_id`` (the /billing page), the builder reads its own
    account and the courtesy lot a super admin granted it; another
    builder's home tenant shape reads only its own (empty) account."""

    from src.api.routes.auth import pwd_context as login_context
    from src.config.settings import settings

    world = await _seed(databases.head)
    home = uuid4()
    email = f"builder-{home.hex[:12]}@example.com"
    password = uuid4().hex
    engine = create_async_engine(databases.head, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                Tenant(
                    id=home,
                    name="Builder home (unlinked)",
                    slug=f"home-{home.hex}",
                    api_key_hash=login_context.hash(uuid4().hex),
                    builder_id=None,
                    config={},
                )
            )
        # The users row in SQL: its role is the PostgreSQL ``userrole`` enum.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, "
                    "role, builder_id, is_active, created_at, updated_at) "
                    "VALUES (:id, :tenant, :email, :hash, 'builder', :builder, "
                    "true, now(), now())"
                ),
                {
                    "id": uuid4(),
                    "tenant": home,
                    "email": email,
                    "hash": login_context.hash(password),
                    "builder": world.builder,
                },
            )
    finally:
        await engine.dispose()
    assert (
        await _scalar(
            databases.head,
            "SELECT builder_id IS NULL FROM tenants WHERE id = :id",
            id=home,
        )
        is True
    )

    async with _running_app(databases.head, monkeypatch) as client:
        grant = await client.post(
            GRANTS,
            json={
                "builder_id": str(world.builder),
                "credits": 5_000,
                "reason": "pg-home",
            },
            headers=_admin(world, "pg-home-grant"),
        )
        assert grant.status_code == 201, grant.text
        lot = grant.json()["lot_id"]

        login = await client.post(
            "/v1/auth/login", json={"email": email, "password": password}
        )
        assert login.status_code == 200, login.text
        claims = jwt.decode(login.json()["token"], options={"verify_signature": False})
        assert (claims["tenant_id"], claims["role"], claims["builder_id"]) == (
            str(home),
            "builder",
            str(world.builder),
        )
        headers = {"Authorization": f"Bearer {login.json()['token']}"}

        summary = await client.get("/v1/credits/summary", headers=headers)
        assert summary.status_code == 200, summary.text
        balance = summary.json()["balance"]
        assert balance["courtesy_available_mc"] == 5_000 * MC
        assert balance["available_mc"] == 5_000 * MC
        lots = await client.get("/v1/credits/lots", headers=headers)
        assert lots.status_code == 200, lots.text
        assert [
            (x["lot_id"], x["kind"], x["available_mc"]) for x in lots.json()["lots"]
        ] == [(lot, "courtesy", 5_000 * MC)]
        own = await client.get(
            "/v1/credits/lots", params={"tenant_id": str(home)}, headers=headers
        )
        assert own.status_code == 200, own.text
        assert own.json()["lots"] == lots.json()["lots"]

        # ANOTHER builder's user on its own unlinked home tenant reads that
        # builder's own account: nothing of the courtesy lot.  (Two builders'
        # users on ONE unlinked tenant are both refused: tested below.)
        other_home, _ = await _unlinked_home(databases.head, "other")
        other_user = await _insert_user(
            databases.head,
            tenant=other_home,
            builder=world.other_builder,
            email=f"other-{home.hex[:12]}@example.com",
            password=uuid4().hex,
        )
        now = datetime.now(timezone.utc)
        other_token = jwt.encode(
            {
                "sub": str(other_user),
                "tenant_id": str(other_home),
                "role": "builder",
                "builder_id": str(world.other_builder),
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            settings.secret_key,
            algorithm="HS256",
        )
        other = {"Authorization": f"Bearer {other_token}"}
        foreign = await client.get("/v1/credits/lots", headers=other)
        assert foreign.status_code == 200, foreign.text
        assert foreign.json()["lots"] == []
        # A clinic override is never the fallback: the seeded tenant of the
        # builder is refused to the other builder's token.
        clinic = await client.get(
            "/v1/credits/summary",
            params={"tenant_id": str(world.tenant)},
            headers=other,
        )
        assert (clinic.status_code, clinic.json()) == (
            403,
            {"detail": {"code": "tenant_override_forbidden"}},
        )


async def _insert_user(
    url: URL,
    *,
    tenant: UUID,
    builder: UUID | None,
    email: str,
    password: str,
    role: str = "builder",
) -> UUID:
    """A user in SQL (role ``builder`` unless ``role`` says otherwise):
    ``users.role`` as the migrations built it."""

    from src.api.routes.auth import pwd_context as login_context

    user_id = uuid4()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, "
                    "role, builder_id, is_active, created_at, updated_at) "
                    "VALUES (:id, :tenant, :email, :hash, :role, :builder, "
                    "true, now(), now())"
                ),
                {
                    "role": role,
                    "id": user_id,
                    "tenant": tenant,
                    "email": email,
                    "hash": login_context.hash(password),
                    "builder": builder,
                },
            )
    finally:
        await engine.dispose()
    return user_id


HOME_CONTENT = "Conteúdo do tenant home"


async def _unlinked_home(url: URL, label: str) -> tuple[UUID, UUID]:
    """A tenant linked to no builder holding one knowledge document;
    returns (tenant, document)."""

    from src.api.middleware.auth import pwd_context

    home, document = uuid4(), uuid4()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                Tenant(
                    id=home,
                    name=f"Builder home (unlinked, {label})",
                    slug=f"home-{label}-{home.hex}",
                    api_key_hash=pwd_context.hash(uuid4().hex),
                    builder_id=None,
                    config={},
                )
            )
            await session.flush()
            session.add(
                KnowledgeDocument(
                    id=document,
                    tenant_id=home,
                    title="Home",
                    content=HOME_CONTENT,
                    content_type="text",
                    document_metadata={},
                )
            )
    finally:
        await engine.dispose()
    return home, document


def _exported_ids(response: Any, home: UUID) -> list[str]:
    """The ids an export answer holds, checked to be ``home``'s archive."""

    assert response.status_code == 200, response.text
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["tenant_id"] == str(home)
    for item in manifest["items"]:
        assert archive.read(item["path"]).decode() == HOME_CONTENT
    return [item["id"] for item in manifest["items"]]


async def _login(client: Any, email: str, password: str) -> dict[str, str]:
    login = await client.post(
        "/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def _absent_builder(builder: UUID) -> dict[str, str]:
    from src.config.settings import settings

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "builder",
            "builder_id": str(builder),
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


_STALE_SQL = {
    "deactivated": "UPDATE users SET is_active = false WHERE id = :id",
    "moved_builder": "UPDATE users SET builder_id = :builder WHERE id = :id",
    "moved_tenant": "UPDATE users SET tenant_id = :tenant WHERE id = :id",
    "deleted": "DELETE FROM users WHERE id = :id",
    "demoted": "UPDATE users SET role = 'clinic_admin' WHERE id = :id",
}
# Headers that legitimately differ between two answers.
_VOLATILE_HEADERS = {
    "date",
    "x-request-id",
    "x-response-time",
    "x-process-time",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
}
FORBIDDEN = {"detail": {"code": "tenant_override_forbidden"}}


def _stable_headers(response: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _VOLATILE_HEADERS
    }


async def test_at_046_a_stale_builder_token_on_an_unlinked_home_tenant_is_refused(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Builder users of an unlinked home tenant log in through the real
    ``/v1/auth/login``; while their ``users`` row is current they read the
    builder's account and export the home tenant's bytes.  Then each row
    goes stale one way; the same tokens get the absent tenant's 403 on the
    credit reads and the export, and the requests run no ``credit_*`` or
    ``billing_*`` statement.  Each user has its OWN home tenant, so no
    staleness is masked by a shared-tenant refusal (only ``moved_builder``,
    whose row itself then names another builder, meets both)."""

    world = await _seed(databases.head)
    credentials: dict[str, tuple[UUID, str, str, UUID, UUID]] = {}
    for stale in _STALE_SQL:
        home, document = await _unlinked_home(databases.head, f"stale-{stale}")
        email = f"stale-{stale}-{home.hex[:8]}@example.com"
        password = uuid4().hex
        user = await _insert_user(
            databases.head,
            tenant=home,
            builder=world.builder,
            email=email,
            password=password,
        )
        credentials[stale] = (user, email, password, home, document)

    async with _running_app(databases.head, monkeypatch) as client:
        tokens: dict[str, dict[str, str]] = {}
        for stale, (_user, email, password, home, document) in credentials.items():
            headers = await _login(client, email, password)
            summary = await client.get("/v1/credits/summary", headers=headers)
            assert summary.status_code == 200, (stale, summary.text)
            export = await client.get("/v1/credits/export", headers=headers)
            assert _exported_ids(export, home) == [str(document)]
            tokens[stale] = headers

        engine = create_async_engine(databases.head, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                for stale, (user, *_rest) in credentials.items():
                    await connection.execute(
                        text(_STALE_SQL[stale]),
                        {
                            "id": user,
                            "builder": world.other_builder,
                            "tenant": world.tenant,
                        },
                    )
        finally:
            await engine.dispose()

        absent = _absent_builder(world.builder)
        paths = ("/v1/credits/summary", "/v1/credits/lots", "/v1/credits/export")
        with _statements() as statements:
            for stale, headers in tokens.items():
                for path in paths:
                    refused = await client.get(path, headers=headers)
                    missing = await client.get(path, headers=absent)
                    assert (refused.status_code, refused.json()) == (
                        403,
                        FORBIDDEN,
                    ), (stale, path, refused.text)
                    assert (missing.status_code, missing.json()) == (403, FORBIDDEN)
                    assert _stable_headers(refused) == _stable_headers(missing)
    requests = _of_requests(statements)
    assert [s for s in requests if "credit_" in s or "billing_" in s] == []
    # Every stale token's request re-read its users row: ONE statement per
    # request (the token's row and the shared-tenant EXISTS together); the
    # absent tenant's requests read none.
    assert len([s for s in requests if " from users " in s]) == len(tokens) * len(paths)


async def test_at_046_an_unlinked_tenant_shared_by_two_builders_is_refused_to_both(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision 2026-11-10 on PostgreSQL: two users of the SAME
    builder and a ``clinic_admin`` user on one unlinked tenant keep the
    fallback (credit read and export); once a builder user of ANOTHER
    builder joins that tenant, all three builder tokens get the absent
    tenant's 403 on the credit reads and the export, with no ``credit_*``
    or ``billing_*`` statement and one ``users`` statement per request."""

    world = await _seed(databases.head)
    home, document = await _unlinked_home(databases.head, "shared")
    users: dict[str, tuple[str, str]] = {}
    for label, builder, role in (
        ("first", world.builder, "builder"),
        ("second", world.builder, "builder"),
        ("clinic", None, "clinic_admin"),
    ):
        email = f"shared-{label}-{home.hex[:8]}@example.com"
        password = uuid4().hex
        await _insert_user(
            databases.head,
            tenant=home,
            builder=builder,
            email=email,
            password=password,
            role=role,
        )
        users[label] = (email, password)

    async with _running_app(databases.head, monkeypatch) as client:
        tokens = {
            label: await _login(client, *users[label]) for label in ("first", "second")
        }
        for label, headers in tokens.items():
            summary = await client.get("/v1/credits/summary", headers=headers)
            assert summary.status_code == 200, (label, summary.text)
            export = await client.get("/v1/credits/export", headers=headers)
            assert _exported_ids(export, home) == [str(document)]

        email = f"shared-other-{home.hex[:8]}@example.com"
        password = uuid4().hex
        await _insert_user(
            databases.head,
            tenant=home,
            builder=world.other_builder,
            email=email,
            password=password,
        )
        tokens["other"] = await _login(client, email, password)

        absent = _absent_builder(world.builder)
        paths = ("/v1/credits/summary", "/v1/credits/lots", "/v1/credits/export")
        with _statements() as statements:
            for path in paths:
                missing = await client.get(path, headers=absent)
                assert (missing.status_code, missing.json()) == (403, FORBIDDEN)
                for label, headers in tokens.items():
                    refused = await client.get(path, headers=headers)
                    assert (refused.status_code, refused.json()) == (
                        403,
                        FORBIDDEN,
                    ), (label, path, refused.text)
                    assert _stable_headers(refused) == _stable_headers(missing)
    requests = _of_requests(statements)
    assert [s for s in requests if "credit_" in s or "billing_" in s] == []
    assert len([s for s in requests if " from users " in s]) == len(tokens) * len(paths)


# ---------------------------------------------------------------------------
# 045: the switch stays on, only the courtesy routes close.
# ---------------------------------------------------------------------------


async def test_at_045_only_the_courtesy_routes_close_and_with_no_sql(
    databases: _Databases, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _seed(databases.at_045)
    async with _running_app(databases.at_045, monkeypatch) as client:
        from src.services.credit_switch import (
            courtesy_gate_open,
            credit_switch_effective,
            credit_switch_reading,
        )

        reading = credit_switch_reading()
        assert credit_switch_effective() is True
        assert (reading.effective, reading.revision) == (True, "045")
        assert courtesy_gate_open(reading) is False

        calls = [
            (
                "POST",
                GRANTS,
                {"builder_id": str(world.builder), "credits": 1, "reason": "x"},
            ),
            (
                "POST",
                f"{GRANTS}/{uuid4()}/removals",
                {"remove_all": True, "reason": "x"},
            ),
            ("GET", f"{GRANTS}?builder_id={world.builder}", None),
        ]
        with _statements() as statements:
            answers = []
            for method, path, body in calls:
                answers.append(
                    await client.request(
                        method, path, json=body, headers=_admin(world, "k-045")
                    )
                )
                answers.append(
                    await client.request(
                        method,
                        path,
                        content=b"{broken",
                        headers={
                            **_admin(world, "k-045"),
                            "Content-Type": "application/json",
                        },
                    )
                )
        for answer in answers:
            assert (answer.status_code, answer.json()) == (503, UNAVAILABLE)
        assert _of_requests(statements) == []

        # Every other credit surface answers on the 045 schema.
        balance = await client.get("/v1/credits/balance", headers=_builder(world))
        assert balance.status_code == 200, balance.text
        assert balance.json()["courtesy_available_mc"] == 0
        binding = await client.get(
            f"/v1/admin/credits/rollout/bindings/{world.builder}",
            headers=_admin(world),
        )
        assert binding.status_code == 200, binding.text
        assert binding.json()["usable_courtesy"] is False
        with _statements() as statements:
            subscriptions = await client.get(
                "/v1/admin/builders/subscriptions", headers=_admin(world)
            )
        assert subscriptions.status_code == 200, subscriptions.text
        rows = subscriptions.json()
        assert rows and all(row["courtesy"] is None for row in rows)
        assert all(
            {"subscription", "subscription_unavailable", "courtesy"} <= set(row)
            for row in rows
        )
        assert [s for s in _of_requests(statements) if "credit_" in s] == []
    assert await _scalar(databases.at_045, "SELECT count(*) FROM credit_lots") == 0


# ---------------------------------------------------------------------------
# Server-log hygiene.  Must stay the LAST test of the module.
# ---------------------------------------------------------------------------


async def test_the_server_log_of_these_databases_holds_no_error(
    databases: _Databases,
) -> None:
    # A WARNING per database proves the log captures them (else "no ERROR"
    # would be vacuous).
    for url in (databases.head, databases.at_045):
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(
                    text("DO $$ BEGIN RAISE WARNING 'courtesy-http-log-probe'; END $$")
                )
        finally:
            await engine.dispose()
    admin = create_async_engine(
        databases.cluster.admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as connection:
            collector, prefix, superuser = (
                await connection.execute(
                    text(
                        "SELECT current_setting('logging_collector'), "
                        "current_setting('log_line_prefix'), "
                        "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
                    )
                )
            ).one()
            if collector != "on" or "%d" not in prefix or not superuser:
                pytest.skip(
                    "server log hygiene needs a superuser, logging_collector=on "
                    f"and %d in log_line_prefix (got {collector!r}, {prefix!r})"
                )
            logfile = await connection.scalar(text("SELECT pg_current_logfile()"))
            content = str(
                await connection.scalar(
                    text("SELECT pg_read_file(:path)"), {"path": logfile}
                )
            )
    finally:
        await admin.dispose()
    mine = [
        line
        for line in content.splitlines()
        if any(f" {name} " in line for name in databases.names)
    ]
    probes = [line for line in mine if "courtesy-http-log-probe" in line]
    assert (
        len(
            {name for name in databases.names for line in probes if f" {name} " in line}
        )
        == 2
    ), "the log does not show both databases: the audit would be vacuous"
    errors = [
        match.group("message")
        for line in mine
        if (match := _ERROR_LINE.search(line)) is not None
    ]
    assert errors == []
