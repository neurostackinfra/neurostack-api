"""The 24-hour media delivery claim and the bridge mappings on real PostgreSQL.

envio-midia-agente Group 2.  One database at ``alembic upgrade head`` (045:
``media_assets.trigger_intents`` and ``media_deliveries``), shared by the
module; every test seeds its own tenant, sessions and media assets.  Pinned
here:

* :func:`claim_media_delivery`: the first claim of a (session, media) pair
  inserts its row and returns its id; a claim within 24 hours returns
  ``None`` and changes nothing; a claim once the last send is 24 hours old
  (``last_sent_at`` moved back in SQL, never a sleep) returns the same row id
  and moves ``last_sent_at`` to ``now()``; the claim never commits;
* two claims of one pair on two real connections, both racing for the first
  insert and for a stale row: exactly one wins, the other waits on its row
  lock and returns ``None`` once it commits (and wins when it rolls back);
* other sessions and other media assets claim independently;
* the claim refuses any other dialect before it sends SQL;
* the ORM never touches ``media_deliveries`` when a parent goes: deleting a
  session, a media asset or a tenant through the ORM leaves the rows to the
  DDL's cascades (no relationship, no attempt to null a ``NOT NULL`` key);
* ORM x DDL: ``MediaDelivery`` and ``MediaAsset.trigger_intents`` equal what
  migration 045 created (reflected from PostgreSQL on both sides), and the
  ORM reads and writes ``TEXT[]`` lists.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  (registers every mapper on Base.metadata)
from src.models.base import Base
from src.models.media_asset import MediaAsset
from src.models.media_delivery import MediaDelivery
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.media_deliveries import (
    MEDIA_DELIVERY_REPEAT_WINDOW,
    MediaDeliveryClaimUnsupported,
    claim_media_delivery,
)
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

_PDF = b"%PDF-1.7\nmedia delivery claim"
_WAIT_SECONDS = 10


@pytest.fixture(scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    migrated: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        migrated = create_async_engine(cluster.url(name), poolclass=NullPool)
        yield migrated
    finally:
        if migrated is not None:
            await migrated.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def _seed(
    engine: AsyncEngine, *, sessions: int = 1, media: int = 1
) -> tuple[UUID, list[UUID], list[UUID]]:
    tenant_id = uuid4()
    session_ids = [uuid4() for _ in range(sessions)]
    media_ids = [uuid4() for _ in range(media)]
    async with _sessions(engine)() as database:
        database.add(
            Tenant(
                id=tenant_id,
                name="Media delivery claim",
                slug=f"media-delivery-{tenant_id.hex}",
                api_key_hash=f"media-delivery-{tenant_id.hex}",
                plan_type="clinic",
                config={},
            )
        )
        await database.flush()
        database.add_all(
            [
                *(
                    Session(
                        id=session_id,
                        tenant_id=tenant_id,
                        channel="whatsapp",
                        cognitive_state={},
                        session_metadata={},
                    )
                    for session_id in session_ids
                ),
                *(
                    MediaAsset(
                        id=media_id,
                        tenant_id=tenant_id,
                        display_name=f"Tabela {index}",
                        original_filename=f"tabela-{index}.pdf",
                        category="pdf",
                        mime_type="application/pdf",
                        size_bytes=len(_PDF),
                        sha256=hashlib.sha256(_PDF).digest(),
                        payload=_PDF,
                        auto_suggest=True,
                        trigger_intents=["detalhes_servicos"],
                    )
                    for index, media_id in enumerate(media_ids)
                ),
            ]
        )
        await database.commit()
    return tenant_id, session_ids, media_ids


async def _claim_and_commit(
    engine: AsyncEngine, tenant_id: UUID, session_id: UUID, media_id: UUID
) -> UUID | None:
    async with _sessions(engine)() as database:
        claimed = await claim_media_delivery(
            database, tenant_id=tenant_id, session_id=session_id, media_id=media_id
        )
        await database.commit()
        return claimed


async def _rows(engine: AsyncEngine, session_id: UUID) -> list[tuple[Any, ...]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT id, tenant_id, media_id, last_sent_at, now() "
                "FROM media_deliveries WHERE session_id = :session ORDER BY media_id"
            ),
            {"session": session_id},
        )
        return [tuple(row) for row in rows]


async def _age(engine: AsyncEngine, session_id: UUID, by: timedelta) -> None:
    """Move the last send of every row of the session to ``now() - by``."""

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "UPDATE media_deliveries SET last_sent_at = now() - CAST(:by AS "
                "interval) WHERE session_id = :session"
            ),
            {"by": by, "session": session_id},
        )


async def _age_and_claim_and_commit(
    engine: AsyncEngine,
    tenant_id: UUID,
    session_id: UUID,
    media_id: UUID,
    by: timedelta,
) -> UUID | None:
    """Age the session's rows to ``now() - by`` and claim, in one transaction.

    ``now()`` is the start of the transaction, so the claim compares against
    the very instant the UPDATE wrote: with ``by`` equal to the window,
    ``last_sent_at`` is exactly ``now() - window``, with no clock drift
    between the two statements.
    """

    async with _sessions(engine)() as database:
        await database.execute(
            text(
                "UPDATE media_deliveries SET last_sent_at = now() - CAST(:by AS "
                "interval) WHERE session_id = :session"
            ),
            {"by": by, "session": session_id},
        )
        claimed = await claim_media_delivery(
            database, tenant_id=tenant_id, session_id=session_id, media_id=media_id
        )
        await database.commit()
        return claimed


# ---------------------------------------------------------------------------
# The claim, one caller at a time.
# ---------------------------------------------------------------------------


async def test_the_first_claim_inserts_the_row_and_returns_its_id(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)

    claimed = await _claim_and_commit(engine, tenant_id, session_id, media_id)

    assert isinstance(claimed, UUID)
    [(row_id, row_tenant, row_media, last_sent_at, now)] = await _rows(
        engine, session_id
    )
    assert (row_id, row_tenant, row_media) == (claimed, tenant_id, media_id)
    assert timedelta(0) <= now - last_sent_at < timedelta(minutes=1)


async def test_a_claim_within_24_hours_returns_none_and_changes_nothing(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)
    first = await _claim_and_commit(engine, tenant_id, session_id, media_id)
    before = await _rows(engine, session_id)

    assert await _claim_and_commit(engine, tenant_id, session_id, media_id) is None
    # Just inside the window: 23 h 59 min ago is still a repeat.
    await _age(engine, session_id, timedelta(hours=23, minutes=59))
    aged = await _rows(engine, session_id)
    assert await _claim_and_commit(engine, tenant_id, session_id, media_id) is None

    after = await _rows(engine, session_id)
    assert [row[:4] for row in after] == [row[:4] for row in aged]
    assert [row[0] for row in after] == [row[0] for row in before] == [first]


async def test_a_claim_after_24_hours_reclaims_the_same_row(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)
    first = await _claim_and_commit(engine, tenant_id, session_id, media_id)
    assert MEDIA_DELIVERY_REPEAT_WINDOW == timedelta(hours=24)
    # One microsecond (timestamptz's resolution) inside the window is still a
    # repeat, measured against the same frozen now().
    inside = timedelta(hours=24) - timedelta(microseconds=1)
    assert (
        await _age_and_claim_and_commit(engine, tenant_id, session_id, media_id, inside)
        is None
    )
    # Exactly the window, against the same frozen now(): the boundary is
    # inclusive (last_sent_at <= now() - 24h); a strict ``<`` refuses this.
    again = await _age_and_claim_and_commit(
        engine, tenant_id, session_id, media_id, timedelta(hours=24)
    )

    assert again == first
    [(row_id, _tenant, _media, last_sent_at, now)] = await _rows(engine, session_id)
    assert row_id == first
    assert timedelta(0) <= now - last_sent_at < timedelta(minutes=1)
    # And the window starts over.
    assert await _claim_and_commit(engine, tenant_id, session_id, media_id) is None


async def test_the_claim_never_commits_for_its_caller(engine: AsyncEngine) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)
    async with _sessions(engine)() as database:
        claimed = await claim_media_delivery(
            database, tenant_id=tenant_id, session_id=session_id, media_id=media_id
        )
        assert claimed is not None
        await database.rollback()

    assert await _rows(engine, session_id) == []
    assert await _claim_and_commit(engine, tenant_id, session_id, media_id) is not None


async def test_other_sessions_and_other_media_claim_independently(
    engine: AsyncEngine,
) -> None:
    tenant_id, (one, two), (pdf, image) = await _seed(engine, sessions=2, media=2)

    claims = [
        await _claim_and_commit(engine, tenant_id, session_id, media_id)
        for session_id in (one, two)
        for media_id in (pdf, image)
    ]

    assert all(isinstance(claim, UUID) for claim in claims)
    assert len(set(claims)) == 4
    assert len(await _rows(engine, one)) == len(await _rows(engine, two)) == 2
    assert await _claim_and_commit(engine, tenant_id, one, pdf) is None
    assert await _claim_and_commit(engine, tenant_id, two, image) is None


async def test_the_claim_refuses_any_other_dialect_before_sending_sql() -> None:
    sqlite = create_async_engine("sqlite+aiosqlite:///:memory:")
    executed: list[str] = []

    from sqlalchemy import event

    event.listen(
        sqlite.sync_engine,
        "before_cursor_execute",
        lambda _c, _cur, statement, *_args: executed.append(statement),
    )
    try:
        async with AsyncSession(sqlite) as database:
            with pytest.raises(MediaDeliveryClaimUnsupported, match="sqlite"):
                await claim_media_delivery(
                    database, tenant_id=uuid4(), session_id=uuid4(), media_id=uuid4()
                )
    finally:
        await sqlite.dispose()
    assert executed == []


# ---------------------------------------------------------------------------
# Two real connections racing for one pair.
# ---------------------------------------------------------------------------


async def _wait_until_blocked(engine: AsyncEngine, application_name: str) -> None:
    """Until the backend named ``application_name`` waits on a lock."""

    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    async with engine.connect() as observer:
        while True:
            waiting = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = "
                    ":name AND wait_event_type = 'Lock'"
                ),
                {"name": application_name},
            )
            if waiting:
                return
            assert asyncio.get_running_loop().time() < deadline, "never blocked"
            await asyncio.sleep(0.05)


async def _race(
    engine: AsyncEngine,
    tenant_id: UUID,
    session_id: UUID,
    media_id: UUID,
    *,
    first_commits: bool,
) -> tuple[UUID | None, UUID | None]:
    """Claim on connection A (held open), then on connection B while A holds
    its row lock; finish A, then B.  Returns (A's claim, B's claim)."""

    second_name = f"g2-claim-{uuid4().hex[:8]}"
    second_engine = create_async_engine(
        engine.url,
        poolclass=NullPool,
        connect_args={"server_settings": {"application_name": second_name}},
    )
    try:
        async with AsyncSession(engine) as first, AsyncSession(second_engine) as second:
            first_claim = await claim_media_delivery(
                first, tenant_id=tenant_id, session_id=session_id, media_id=media_id
            )

            async def second_claim() -> UUID | None:
                claimed = await claim_media_delivery(
                    second,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    media_id=media_id,
                )
                await second.commit()
                return claimed

            task = asyncio.create_task(second_claim())
            try:
                await _wait_until_blocked(engine, second_name)
                assert not task.done()
                if first_commits:
                    await first.commit()
                else:
                    await first.rollback()
                second_result = await asyncio.wait_for(task, _WAIT_SECONDS)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        return first_claim, second_result
    finally:
        await second_engine.dispose()


async def test_two_concurrent_first_claims_have_exactly_one_winner(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)

    first, second = await _race(
        engine, tenant_id, session_id, media_id, first_commits=True
    )

    assert isinstance(first, UUID) and second is None
    assert [row[0] for row in await _rows(engine, session_id)] == [first]


async def test_two_concurrent_claims_of_a_stale_row_have_exactly_one_winner(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)
    original = await _claim_and_commit(engine, tenant_id, session_id, media_id)
    await _age(engine, session_id, timedelta(hours=25))

    first, second = await _race(
        engine, tenant_id, session_id, media_id, first_commits=True
    )

    assert (first, second) == (original, None)
    [(_id, _tenant, _media, last_sent_at, now)] = await _rows(engine, session_id)
    assert now - last_sent_at < timedelta(minutes=1)


async def test_the_waiting_claim_wins_when_the_first_one_rolls_back(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)

    first, second = await _race(
        engine, tenant_id, session_id, media_id, first_commits=False
    )

    assert isinstance(first, UUID) and isinstance(second, UUID)
    assert first != second
    assert [row[0] for row in await _rows(engine, session_id)] == [second]


async def test_many_simultaneous_claims_have_exactly_one_winner(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)

    results = await asyncio.gather(
        *(_claim_and_commit(engine, tenant_id, session_id, media_id) for _ in range(8))
    )

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert [row[0] for row in await _rows(engine, session_id)] == winners


# ---------------------------------------------------------------------------
# Parents deleted through the ORM: the DDL cascades, the ORM stays out.
# ---------------------------------------------------------------------------


async def _delivery_count(engine: AsyncEngine, tenant_id: UUID) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text("SELECT count(*) FROM media_deliveries WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        )


async def test_orm_deletes_of_a_session_or_media_leave_the_rows_to_the_cascade(
    engine: AsyncEngine,
) -> None:
    tenant_id, (one, two), (pdf, image) = await _seed(engine, sessions=2, media=2)
    for session_id in (one, two):
        for media_id in (pdf, image):
            await _claim_and_commit(engine, tenant_id, session_id, media_id)
    assert await _delivery_count(engine, tenant_id) == 4

    async with _sessions(engine)() as database:
        await database.delete(await database.get(Session, one))
        await database.commit()
    assert {row[2] for row in await _rows(engine, one)} == set()
    assert await _delivery_count(engine, tenant_id) == 2

    async with _sessions(engine)() as database:
        await database.delete(await database.get(MediaAsset, pdf))
        await database.commit()
    assert [row[2] for row in await _rows(engine, two)] == [image]


async def test_an_orm_tenant_delete_leaves_its_rows_to_the_cascade(
    engine: AsyncEngine,
) -> None:
    tenant_id, (session_id,), (media_id,) = await _seed(engine)
    survivor_tenant, (survivor_session,), (survivor_media,) = await _seed(engine)
    await _claim_and_commit(engine, tenant_id, session_id, media_id)
    await _claim_and_commit(engine, survivor_tenant, survivor_session, survivor_media)

    async with _sessions(engine)() as database:
        await database.delete(await database.get(Tenant, tenant_id))
        await database.commit()

    assert await _delivery_count(engine, tenant_id) == 0
    assert await _delivery_count(engine, survivor_tenant) == 1


def test_no_relationship_reaches_media_deliveries() -> None:
    for mapper in Base.registry.mappers:
        for relationship in mapper.relationships:
            assert relationship.mapper.local_table.name != "media_deliveries", (
                mapper.local_table.name,
                relationship.key,
            )
    assert not inspect(MediaDelivery).relationships


# ---------------------------------------------------------------------------
# ORM x DDL: the mappings equal what migration 045 created.
# ---------------------------------------------------------------------------


def _reflect(sync_connection: Any, table: str, schema: str) -> dict[str, Any]:
    inspector = inspect(sync_connection)
    return {
        "columns": {
            column["name"]: (
                str(column["type"].compile(dialect=sync_connection.dialect)),
                bool(column["nullable"]),
                column.get("default"),
            )
            for column in inspector.get_columns(table, schema=schema)
        },
        "primary_key": tuple(
            inspector.get_pk_constraint(table, schema=schema)["constrained_columns"]
        ),
        "foreign_keys": sorted(
            (
                item["name"],
                tuple(item["constrained_columns"]),
                item["referred_table"],
                tuple(item["referred_columns"]),
                (item.get("options") or {}).get("ondelete"),
            )
            for item in inspector.get_foreign_keys(table, schema=schema)
        ),
        "uniques": sorted(
            (item["name"], tuple(item["column_names"]))
            for item in inspector.get_unique_constraints(table, schema=schema)
        ),
        "indexes": sorted(
            (item["name"], tuple(item["column_names"]), bool(item["unique"]))
            for item in inspector.get_indexes(table, schema=schema)
            if not item.get("duplicates_constraint")
        ),
    }


def _orm_side(sync_connection: Any) -> dict[str, Any]:
    schema = f"g2_orm_{uuid4().hex[:12]}"
    sync_connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    # Unqualified names resolve in the scratch schema first, then public
    # (tenants and sessions stay there).
    sync_connection.exec_driver_sql(f'SET LOCAL search_path TO "{schema}", public')
    try:
        Base.metadata.tables["media_assets"].create(sync_connection)
        Base.metadata.tables["media_deliveries"].create(sync_connection)
        return {
            "media_deliveries": _reflect(sync_connection, "media_deliveries", schema),
            "trigger_intents": _reflect(sync_connection, "media_assets", schema)[
                "columns"
            ]["trigger_intents"],
        }
    finally:
        sync_connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


async def test_the_orm_mappings_equal_the_045_ddl(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        expected = await connection.run_sync(_orm_side)
        await connection.rollback()
        actual = {
            "media_deliveries": await connection.run_sync(
                lambda sync: _reflect(sync, "media_deliveries", "public")
            ),
            "trigger_intents": (
                await connection.run_sync(
                    lambda sync: _reflect(sync, "media_assets", "public")
                )
            )["columns"]["trigger_intents"],
        }

    assert actual == expected
    # Not vacuous: what migration 045 promised.
    assert actual["trigger_intents"] == ("TEXT[]", False, "'{}'::text[]")
    deliveries = actual["media_deliveries"]
    assert deliveries["columns"] == {
        "id": ("UUID", False, None),
        "tenant_id": ("UUID", False, None),
        "session_id": ("UUID", False, None),
        "media_id": ("UUID", False, None),
        "last_sent_at": ("TIMESTAMP WITH TIME ZONE", False, None),
    }
    assert deliveries["primary_key"] == ("id",)
    assert deliveries["foreign_keys"] == [
        (
            "fk_media_deliveries_media",
            ("media_id",),
            "media_assets",
            ("id",),
            "CASCADE",
        ),
        (
            "fk_media_deliveries_session",
            ("session_id",),
            "sessions",
            ("id",),
            "CASCADE",
        ),
        ("fk_media_deliveries_tenant", ("tenant_id",), "tenants", ("id",), "CASCADE"),
    ]
    assert deliveries["uniques"] == [
        ("uq_media_deliveries_session_media", ("session_id", "media_id"))
    ]
    assert deliveries["indexes"] == [
        ("ix_media_deliveries_media", ("media_id",), False),
        ("ix_media_deliveries_tenant", ("tenant_id",), False),
    ]


async def test_the_orm_reads_and_writes_trigger_intents_as_text_arrays(
    engine: AsyncEngine,
) -> None:
    tenant_id, _sessions_ids, (media_id,) = await _seed(engine)
    async with _sessions(engine)() as database:
        asset = await database.get(MediaAsset, media_id)
        assert asset is not None
        assert asset.trigger_intents == ["detalhes_servicos"]
        asset.trigger_intents = ["interesse_tratamento", "pedido_preco"]
        await database.commit()

    async with engine.connect() as connection:
        stored = await connection.scalar(
            text(
                "SELECT trigger_intents = "
                "ARRAY['interesse_tratamento', 'pedido_preco']::text[] "
                "AND pg_typeof(trigger_intents) = 'text[]'::regtype "
                "FROM media_assets WHERE id = :id"
            ),
            {"id": media_id},
        )
    assert stored is True
    # The array operators the selection will use work on the mapping.
    async with _sessions(engine)() as database:
        bound = (
            (
                await database.execute(
                    select(MediaAsset.id).where(
                        MediaAsset.tenant_id == tenant_id,
                        MediaAsset.trigger_intents.contains(["pedido_preco"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert bound == [media_id]


async def test_an_orm_insert_without_intents_stores_the_empty_array(
    engine: AsyncEngine,
) -> None:
    tenant_id, _sessions_ids, _media = await _seed(engine)
    media_id = uuid4()
    async with _sessions(engine)() as database:
        database.add(
            MediaAsset(
                id=media_id,
                tenant_id=tenant_id,
                display_name="Sem assunto",
                original_filename="sem-assunto.pdf",
                category="pdf",
                mime_type="application/pdf",
                size_bytes=len(_PDF),
                sha256=hashlib.sha256(_PDF).digest(),
                payload=_PDF,
            )
        )
        await database.commit()
    async with engine.connect() as connection:
        empty = await connection.scalar(
            text("SELECT trigger_intents = '{}' FROM media_assets WHERE id = :id"),
            {"id": media_id},
        )
    assert empty is True
