"""The "Cobrança ativa" badge over HTTP on real PostgreSQL
(cobranca-credito-por-builder Group 5).

The real application (lifespan, middleware, pool) on a disposable
PostgreSQL database at head (047), with ``credit_hot_path_enabled`` on and
the REAL credit switch reading, so the 047 gate opens on the
``alembic_version`` the lifespan reads:

* the ``charging`` field of ``GET /v1/admin/builders/subscriptions`` is
  ``{"marked_at": "...Z"}`` for the marked builder and ``null`` for the
  unmarked one, from exactly ONE batch read of the marks (the shape of
  ``CreditRepository.latest_enforcement_marks``: a ``row_number()`` per
  builder over ``credit_enforcement_marks`` filtered by a builder list) --
  other readers of the table (the request path, the workers) may run in
  the same window and are not counted;
* a database error in the marks read is rolled back to its savepoint: on
  PostgreSQL an aborted transaction would refuse every later statement, so
  every builder's subscription read would come back unavailable.  Here the
  response is 200, every ``charging`` is null and no row is
  ``subscription_unavailable``.

Each test runs on a fresh database of its own (the deliberate error stays out
of every other database's server log).  Needs ``POSTGRES_SMOKE_DATABASE_URL``;
without it the module skips, and only ``scripts/check-credit-integration.sh``
turns that skip into a failure.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_credit_courtesy_http_postgres import (
    _admin,
    _of_requests,
    _running_app,
    _scalar,
    _seed,
    _statements,
    _World,
)
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

SUBSCRIPTIONS = "/v1/admin/builders/subscriptions"
MARKED_AT = datetime(2026, 11, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
async def cluster() -> AsyncIterator[_Cluster]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    created = _Cluster(admin_url=admin_url, admin=admin, created=[])
    try:
        yield created
    finally:
        for name in list(reversed(created.created)):
            await created.drop(name)
        await admin.dispose()


async def _fresh_head(cluster: _Cluster) -> tuple[URL, _World]:
    """A fresh database at head (047) with the courtesy world seeded and
    ``world.builder`` marked (one ``mark`` event); ``world.other_builder``
    has no mark."""

    url = cluster.url(await cluster.fresh())
    await _migrate(url, "upgrade", "head")
    assert await _scalar(url, "SELECT version_num FROM alembic_version") == "047"
    world = await _seed(url)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO credit_enforcement_marks "
                    "(id, builder_id, kind, occurred_at, actor_user_id, reason) "
                    "VALUES (:id, :builder, 'mark', :at, :actor, 'badge pg')"
                ),
                {
                    "id": uuid4(),
                    "builder": world.builder,
                    "at": MARKED_AT,
                    "actor": world.admin_user,
                },
            )
    finally:
        await engine.dispose()
    return url, world


def _is_badge_batch_read(statement: str) -> bool:
    """The one multi-builder statement of ``latest_enforcement_marks`` (a
    statement as ``_statements`` records it: lower case, spaces collapsed):
    the latest event per builder by ``row_number()`` partitioned by builder,
    over a builder ``IN`` list.  No other reader of the table has this shape:
    ``effective_enforcement``, ``latest_enforcement_mark`` and the window
    state read one builder (``=``, ``LIMIT``), ``any_enforcement_mark``
    reads any row, and ``marked_builders`` (Group 3's workers) ranks every
    builder the same way but with no builder list."""

    return (
        "from credit_enforcement_marks" in statement
        and "row_number() over (partition by credit_enforcement_marks.builder_id"
        in statement
        and "credit_enforcement_marks.builder_id in (" in statement
    )


def _rows(response: Any, world: _World) -> dict[UUID, dict[str, Any]]:
    assert response.status_code == 200, response.text
    rows = {UUID(row["id"]): row for row in response.json()}
    assert {world.builder, world.other_builder} <= set(rows)
    return rows


async def test_the_badge_is_one_statement_and_a_utc_instant(
    cluster: _Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, world = await _fresh_head(cluster)
    async with _running_app(url, monkeypatch) as client:
        with _statements() as statements:
            response = await client.get(SUBSCRIPTIONS, headers=_admin(world))
        rows = _rows(response, world)
    marked = rows[world.builder]["charging"]
    assert marked is not None
    assert set(marked) == {"marked_at"}
    assert marked["marked_at"].endswith("Z"), marked
    assert datetime.fromisoformat(marked["marked_at"]) == MARKED_AT
    assert rows[world.other_builder]["charging"] is None
    for row in rows.values():
        assert row["subscription_unavailable"] is False
    # Only the badge's batch read is counted: another reader of the same
    # table running in this window (Group 3's workers, the request path)
    # must neither make this flaky nor hide a second batch read.
    reads = [s for s in _of_requests(statements) if _is_badge_batch_read(s)]
    assert len(reads) == 1, reads


async def test_a_failed_marks_read_is_contained_by_its_savepoint(
    cluster: _Cluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.repositories.credit import CreditRepository

    url, world = await _fresh_head(cluster)

    async def broken(self: Any, *_args: Any, **_kwargs: Any) -> Any:
        await self.session.execute(text("SELECT * FROM no_such_marks_table"))

    monkeypatch.setattr(CreditRepository, "latest_enforcement_marks", broken)
    async with _running_app(url, monkeypatch) as client:
        response = await client.get(SUBSCRIPTIONS, headers=_admin(world))
        rows = _rows(response, world)
    for row in rows.values():
        assert row["charging"] is None
        # The transaction was not aborted: every later read still ran.
        assert (row["subscription"], row["subscription_unavailable"]) == (
            None,
            False,
        )
