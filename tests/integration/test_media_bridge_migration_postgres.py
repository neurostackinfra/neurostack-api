"""Migration 045, the media bridge release, on real PostgreSQL (envio-midia-agente G1).

PostgreSQL only.  A module template at ``042`` with rows that existed before
the bridge (a tenant, two sessions, two media assets), cloned per test and
upgraded to ``045`` there, as window 045 does: ``043`` and ``044`` (the media
category/MIME invariant widened for ``audio/ogg`` and ``audio/mp4``, no
windows of their own) and ``045`` in one run, one transaction.  Pinned here:

* the upgrade adds ``media_assets.trigger_intents TEXT[] NOT NULL DEFAULT
  '{}'``: every row that existed at 042 reads ``'{}'``, and so does an insert
  that does not name the column (the ORM of the bridge image maps neither
  object, so its inserts never do);
* the table ``media_deliveries``: its columns, ``UNIQUE (session_id,
  media_id)`` (23505, and usable as the ``ON CONFLICT`` arbiter of the
  behaviour release's claim) and the three foreign keys ``ON DELETE
  CASCADE``, proven at the SQL level: deleting the tenant, the session or the
  media asset deletes the row, and only that row; a delivery of a tenant that
  does not exist is refused (23503, ``fk_media_deliveries_tenant``);
* the downgrade restores the 044 catalog, then the 042 catalog, exactly
  (042 -> 045 -> 044 -> 042 -> 045 on an empty database);
* the window's one transaction: behind a writer of ``sessions`` or
  ``tenants`` it gives up with nothing of 043, 044 or 045 left, and while it
  waits for that writer it already holds ``ACCESS EXCLUSIVE`` on
  ``media_assets`` (a reader gives up, 55P03); an ``audio/ogg`` or
  ``audio/mp4`` asset makes the downgrade below 044 fail (23514,
  ``ck_media_assets_category_mime``) with 045's objects intact;
* the window's preflight SQL reads every count 0 only at the 042 catalog:
  its media check row is 1 for a widened, missing or drifted constraint;
* the downgrade refuses (SQLSTATE 55000, nothing changed) while
  ``media_deliveries`` holds a row, while a media asset has trigger intents,
  and on another revision; both directions give up after the lock timeout
  behind a reader of ``media_assets`` and change nothing, and the upgrade
  gives up too behind a writer of ``sessions`` or ``tenants`` (the foreign
  keys' SHARE ROW EXCLUSIVE lock conflicts with it);
* no ORM model maps ``trigger_intents`` or ``media_deliveries`` (the bridge
  image runs on either schema).

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.models.base import Base
from tests.alembic_graph import heads, migration_revisions
from tests.integration.test_asaas_billing_postgres import (
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_migration_036_postgres import (
    _LOCK_BOUND_SECONDS,
    _alembic,
    _alembic_while_reading,
    _assert_lock_timeout_in_the_preflight,
    _catalog,
    _Cluster,
    _execute,
    _migrate,
    _refusal,
    _scalar,
    _spawn_alembic,
)

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _ROOT
    / "alembic"
    / "versions"
    / "20261107_000002_045_add_media_trigger_intents_and_deliveries.py"
)
_DOWNGRADE_REFUSED = "downgrade of migration 045 refused (nothing was changed): "

_TENANT = UUID("00000000-0000-4000-8000-000000000431")
_SESSION_ONE = UUID("00000000-0000-4000-8000-000000004311")
_SESSION_TWO = UUID("00000000-0000-4000-8000-000000004312")
_MEDIA_ONE = UUID("00000000-0000-4000-8000-000000004321")
_MEDIA_TWO = UUID("00000000-0000-4000-8000-000000004322")
_PDF = b"%PDF"


def _load_migration() -> ModuleType:
    """The migration module itself (its preflight SQL, run directly).

    In-process, the repository's ``alembic/`` directory may shadow the
    package; ``op`` is only needed as a name for the module to import.
    """

    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_045", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration()

_INSERT_TENANT = (
    "INSERT INTO tenants (id, name, api_key_hash, is_active) "
    "VALUES (:id, 'G1 media bridge', 'g1-media-bridge-hash', true)"
)
_INSERT_SESSION = (
    "INSERT INTO sessions (id, tenant_id, channel, is_active, message_count) "
    "VALUES (:id, :tenant, 'whatsapp', true, 0)"
)
# Without trigger_intents: the column the bridge image never names.
_INSERT_MEDIA = (
    "INSERT INTO media_assets (id, tenant_id, display_name, original_filename, "
    "category, mime_type, size_bytes, sha256, payload) VALUES (:id, :tenant, "
    "'Tabela', 'tabela.pdf', 'pdf', 'application/pdf', 4, :sha256, :payload)"
)
_INSERT_DELIVERY = (
    "INSERT INTO media_deliveries (id, tenant_id, session_id, media_id, "
    "last_sent_at) VALUES (gen_random_uuid(), :tenant, :session, :media, now())"
)


async def _seed_media(cluster: _Cluster, name: str, media: UUID) -> None:
    await _execute(
        cluster,
        name,
        _INSERT_MEDIA,
        id=media,
        tenant=_TENANT,
        sha256=b"\x01" * 32,
        payload=_PDF,
    )


async def _deliver(cluster: _Cluster, name: str, session: UUID, media: UUID) -> None:
    await _execute(
        cluster, name, _INSERT_DELIVERY, tenant=_TENANT, session=session, media=media
    )


async def _deliveries(cluster: _Cluster, name: str) -> list[tuple[UUID, UUID]]:
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT session_id, media_id FROM media_deliveries "
                    "ORDER BY session_id, media_id"
                )
            )
            return [(row[0], row[1]) for row in rows]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Module fixture: one 042 template with the rows that existed before 045.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Baseline:
    cluster: _Cluster
    template_042: str
    catalog_042: dict
    catalog_044: dict


@pytest.fixture(scope="module")
async def baseline() -> AsyncIterator[_Baseline]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    try:
        template = await cluster.fresh()
        await _migrate(cluster.url(template), "upgrade", "042")
        await _execute(cluster, template, _INSERT_TENANT, id=_TENANT)
        for session in (_SESSION_ONE, _SESSION_TWO):
            await _execute(
                cluster, template, _INSERT_SESSION, id=session, tenant=_TENANT
            )
        for media in (_MEDIA_ONE, _MEDIA_TWO):
            await _seed_media(cluster, template, media)
        catalog_042 = await _catalog(cluster, template)
        at_044 = await cluster.clone(template)
        await _migrate(cluster.url(at_044), "upgrade", "044")
        catalog_044 = await _catalog(cluster, at_044)
        await cluster.drop(at_044)
        yield _Baseline(cluster, template, catalog_042, catalog_044)
    finally:
        for name in list(reversed(cluster.created)):
            await cluster.drop(name)
        await admin.dispose()


@pytest.fixture
async def at_042(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_042)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


@pytest.fixture
async def at_045(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_042)
    try:
        await _migrate(baseline.cluster.url(name), "upgrade", "045")
        yield name
    finally:
        await baseline.cluster.drop(name)


# ---------------------------------------------------------------------------
# The revision and the ORM (no database).
# ---------------------------------------------------------------------------


def test_045_revises_044_and_is_the_single_head() -> None:
    assert (_MIGRATION.revision, _MIGRATION.down_revision) == ("045", "044")
    revisions = migration_revisions()
    assert revisions[_MIGRATION_PATH.name] == ("045", "044")
    assert heads(revisions) == ["045"]
    # The window's chain: 043 and 044 (the media widenings) below 045.
    parents = dict(revisions.values())
    assert (parents["045"], parents["044"], parents["043"]) == ("044", "043", "042")
    # Not a credit migration: the credit switch's minimum stays 042.
    assert "_credit_" not in _MIGRATION_PATH.name
    assert _MIGRATION.CREATED_TABLES == ("media_deliveries",)
    assert _MIGRATION.ADDED_COLUMNS == (("media_assets", "trigger_intents"),)


def test_no_orm_model_maps_the_bridge_objects() -> None:
    """The bridge image runs on 042 and on 045: its ORM names neither
    object (the behaviour release maps them after the window)."""

    import src.models  # noqa: F401  (registers every model on Base.metadata)

    assert "media_deliveries" not in Base.metadata.tables
    assert "trigger_intents" not in Base.metadata.tables["media_assets"].columns


# ---------------------------------------------------------------------------
# The upgrade.
# ---------------------------------------------------------------------------


async def test_rows_that_existed_at_042_read_an_empty_trigger_intents(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    engine = create_async_engine(cluster.url(at_045), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text("SELECT id, trigger_intents FROM media_assets ORDER BY id")
                )
            ).all()
            column = (
                await connection.execute(
                    text(
                        "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                        "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute AS a "
                        "JOIN pg_attrdef AS d ON d.adrelid = a.attrelid "
                        "AND d.adnum = a.attnum WHERE a.attrelid = "
                        "'public.media_assets'::regclass "
                        "AND a.attname = 'trigger_intents'"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert [(row[0], row[1]) for row in rows] == [(_MEDIA_ONE, []), (_MEDIA_TWO, [])]
    assert tuple(column) == ("text[]", True, "'{}'::text[]")
    # An insert that does not name the column (the bridge image's) gets '{}'.
    later = UUID("00000000-0000-4000-8000-000000004323")
    await _seed_media(cluster, at_045, later)
    assert (
        await _scalar(
            cluster,
            at_045,
            "SELECT trigger_intents = '{}' FROM media_assets WHERE id = :id",
            id=later,
        )
        is True
    )
    with pytest.raises(DBAPIError) as info:
        await _execute(
            cluster,
            at_045,
            "UPDATE media_assets SET trigger_intents = NULL WHERE id = :id",
            id=later,
        )
    assert _sqlstate(info.value) == "23502"


async def test_media_deliveries_is_exactly_the_bridge_table(
    baseline: _Baseline, at_045: str
) -> None:
    catalog = await _catalog(baseline.cluster, at_045)
    columns = [row[1:4] for row in catalog["columns"] if row[0] == "media_deliveries"]
    assert sorted(columns) == sorted(
        [
            ("id", "uuid", True),
            ("tenant_id", "uuid", True),
            ("session_id", "uuid", True),
            ("media_id", "uuid", True),
            ("last_sent_at", "timestamp with time zone", True),
        ]
    )
    constraints = {
        row[1]: (row[2], row[3])
        for row in catalog["constraints"]
        if row[0] == "media_deliveries"
    }
    assert constraints == {
        "media_deliveries_pkey": ("p", "PRIMARY KEY (id)"),
        "uq_media_deliveries_session_media": ("u", "UNIQUE (session_id, media_id)"),
        "fk_media_deliveries_tenant": (
            "f",
            "FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE",
        ),
        "fk_media_deliveries_session": (
            "f",
            "FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE",
        ),
        "fk_media_deliveries_media": (
            "f",
            "FOREIGN KEY (media_id) REFERENCES media_assets(id) ON DELETE CASCADE",
        ),
    }
    assert (
        await _scalar(
            baseline.cluster,
            at_045,
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname = 'ix_media_deliveries_media'",
        )
        == "CREATE INDEX ix_media_deliveries_media ON public.media_deliveries "
        "USING btree (media_id)"
    )
    # A tenant delete cascades through tenant_id: indexed, like the other
    # tenant-scoped session children.
    assert (
        await _scalar(
            baseline.cluster,
            at_045,
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname = 'ix_media_deliveries_tenant'",
        )
        == "CREATE INDEX ix_media_deliveries_tenant ON public.media_deliveries "
        "USING btree (tenant_id)"
    )


async def test_deleting_a_session_deletes_only_its_deliveries(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    for session in (_SESSION_ONE, _SESSION_TWO):
        for media in (_MEDIA_ONE, _MEDIA_TWO):
            await _deliver(cluster, at_045, session, media)
    await _execute(
        cluster, at_045, "DELETE FROM sessions WHERE id = :id", id=_SESSION_ONE
    )
    assert await _deliveries(cluster, at_045) == [
        (_SESSION_TWO, _MEDIA_ONE),
        (_SESSION_TWO, _MEDIA_TWO),
    ]
    assert await _scalar(cluster, at_045, "SELECT count(*) FROM media_assets") == 2


async def test_deleting_a_media_asset_deletes_only_its_deliveries(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    for session in (_SESSION_ONE, _SESSION_TWO):
        for media in (_MEDIA_ONE, _MEDIA_TWO):
            await _deliver(cluster, at_045, session, media)
    await _execute(
        cluster, at_045, "DELETE FROM media_assets WHERE id = :id", id=_MEDIA_TWO
    )
    assert await _deliveries(cluster, at_045) == [
        (_SESSION_ONE, _MEDIA_ONE),
        (_SESSION_TWO, _MEDIA_ONE),
    ]
    assert await _scalar(cluster, at_045, "SELECT count(*) FROM sessions") == 2


async def test_deleting_a_tenant_deletes_only_its_deliveries(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    other = UUID("00000000-0000-4000-8000-000000000432")
    other_session = UUID("00000000-0000-4000-8000-000000004313")
    other_media = UUID("00000000-0000-4000-8000-000000004324")
    await _execute(
        cluster,
        at_045,
        "INSERT INTO tenants (id, name, api_key_hash, is_active) "
        "VALUES (:id, 'G1 media bridge other', 'g1-media-bridge-other', true)",
        id=other,
    )
    await _execute(cluster, at_045, _INSERT_SESSION, id=other_session, tenant=other)
    await _execute(
        cluster,
        at_045,
        _INSERT_MEDIA,
        id=other_media,
        tenant=other,
        sha256=b"\x02" * 32,
        payload=_PDF,
    )
    await _deliver(cluster, at_045, _SESSION_ONE, _MEDIA_ONE)
    await _execute(
        cluster,
        at_045,
        _INSERT_DELIVERY,
        tenant=other,
        session=other_session,
        media=other_media,
    )
    # Delete only the delivery's tenant link: the cascade from tenants itself,
    # not through its sessions or media assets.
    await _execute(
        cluster,
        at_045,
        "ALTER TABLE media_deliveries DROP CONSTRAINT fk_media_deliveries_session, "
        "DROP CONSTRAINT fk_media_deliveries_media",
    )
    await _execute(cluster, at_045, "DELETE FROM tenants WHERE id = :id", id=other)
    assert await _deliveries(cluster, at_045) == [(_SESSION_ONE, _MEDIA_ONE)]
    assert (
        await _scalar(
            cluster,
            at_045,
            "SELECT count(*) FROM media_deliveries WHERE tenant_id = :id",
            id=_TENANT,
        )
        == 1
    )


async def test_a_delivery_of_an_unknown_tenant_is_refused(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    unknown = UUID("00000000-0000-4000-8000-000000000439")
    with pytest.raises(DBAPIError) as info:
        await _execute(
            cluster,
            at_045,
            _INSERT_DELIVERY,
            tenant=unknown,
            session=_SESSION_ONE,
            media=_MEDIA_ONE,
        )
    assert _sqlstate(info.value) == "23503"
    assert "fk_media_deliveries_tenant" in str(info.value)
    assert await _deliveries(cluster, at_045) == []


async def test_a_session_and_media_pair_is_delivered_at_most_once(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    await _deliver(cluster, at_045, _SESSION_ONE, _MEDIA_ONE)
    with pytest.raises(DBAPIError) as info:
        await _deliver(cluster, at_045, _SESSION_ONE, _MEDIA_ONE)
    assert _sqlstate(info.value) == "23505"
    assert "uq_media_deliveries_session_media" in str(info.value)
    # The pair with another session or another media asset is a new row.
    await _deliver(cluster, at_045, _SESSION_TWO, _MEDIA_ONE)
    await _deliver(cluster, at_045, _SESSION_ONE, _MEDIA_TWO)
    assert len(await _deliveries(cluster, at_045)) == 3
    # The constraint is the arbiter of the behaviour release's claim: a send
    # within 24 h claims nothing, an older one is re-claimed in place.
    claim = (
        "INSERT INTO media_deliveries AS delivery (id, tenant_id, session_id, "
        "media_id, last_sent_at) VALUES (gen_random_uuid(), :tenant, :session, "
        ":media, now()) ON CONFLICT (session_id, media_id) DO UPDATE SET "
        "last_sent_at = EXCLUDED.last_sent_at WHERE delivery.last_sent_at <= "
        "now() - interval '24 hours' RETURNING delivery.id"
    )
    params = {"tenant": _TENANT, "session": _SESSION_ONE, "media": _MEDIA_ONE}
    assert await _scalar(cluster, at_045, claim, **params) is None
    await _execute(
        cluster,
        at_045,
        "UPDATE media_deliveries SET last_sent_at = now() - interval '25 hours' "
        "WHERE session_id = :session AND media_id = :media",
        session=_SESSION_ONE,
        media=_MEDIA_ONE,
    )
    assert await _scalar(cluster, at_045, claim, **params) is not None
    assert len(await _deliveries(cluster, at_045)) == 3


# ---------------------------------------------------------------------------
# The downgrade.
# ---------------------------------------------------------------------------


async def test_the_empty_round_trip_restores_044_then_042_exactly(
    baseline: _Baseline, at_045: str
) -> None:
    """042 -> 045 (the fixture, the window's run) -> 044 -> 042 -> 045: the
    downgrade of 045 restores 044's catalog, and those of 044 and 043 (no
    audio/ogg or audio/mp4 asset) the 042 one, exactly."""

    cluster = baseline.cluster
    await _migrate(cluster.url(at_045), "downgrade", "044")
    assert await _catalog(cluster, at_045) == baseline.catalog_044
    await _migrate(cluster.url(at_045), "downgrade", "042")
    assert await _catalog(cluster, at_045) == baseline.catalog_042
    await _migrate(cluster.url(at_045), "upgrade", "045")
    assert (await _catalog(cluster, at_045))["alembic_version"] == [("045",)]
    assert (
        await _scalar(
            cluster,
            at_045,
            "SELECT count(*) FROM media_assets WHERE trigger_intents = '{}'",
        )
        == 2
    )


async def test_a_delivery_refuses_the_downgrade(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    await _deliver(cluster, at_045, _SESSION_ONE, _MEDIA_ONE)
    before = await _catalog(cluster, at_045)
    code, output = await _alembic(cluster.url(at_045), "downgrade", "042")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        f"{_DOWNGRADE_REFUSED}tables that have rows: media_deliveries"
    )
    assert await _catalog(cluster, at_045) == before
    assert before["alembic_version"] == [("045",)]
    assert len(await _deliveries(cluster, at_045)) == 1


async def test_a_bound_trigger_intent_refuses_the_downgrade(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    await _execute(
        cluster,
        at_045,
        "UPDATE media_assets SET trigger_intents = ARRAY['detalhes_servicos'] "
        "WHERE id = :id",
        id=_MEDIA_ONE,
    )
    await _deliver(cluster, at_045, _SESSION_ONE, _MEDIA_ONE)
    before = await _catalog(cluster, at_045)
    code, output = await _alembic(cluster.url(at_045), "downgrade", "042")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        f"{_DOWNGRADE_REFUSED}tables that have rows: media_deliveries; "
        "columns that hold values: media_assets.trigger_intents"
    )
    assert await _catalog(cluster, at_045) == before
    # The bound intent alone refuses it too.
    await _execute(cluster, at_045, "DELETE FROM media_deliveries")
    code, output = await _alembic(cluster.url(at_045), "downgrade", "042")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        f"{_DOWNGRADE_REFUSED}columns that hold values: media_assets.trigger_intents"
    )
    assert await _scalar(
        cluster,
        at_045,
        "SELECT trigger_intents FROM media_assets WHERE id = :id",
        id=_MEDIA_ONE,
    ) == ["detalhes_servicos"]


@pytest.mark.parametrize(
    "statement,listed",
    [
        ("UPDATE alembic_version SET version_num = '042'", "042"),
        ("DELETE FROM alembic_version", "empty"),
        ("INSERT INTO alembic_version VALUES ('042')", "042, 045"),
    ],
)
async def test_the_downgrade_preflight_refuses_another_revision(
    baseline: _Baseline, at_045: str, statement: str, listed: str
) -> None:
    cluster = baseline.cluster
    await _execute(cluster, at_045, statement)
    before = await _catalog(cluster, at_045)
    engine = create_async_engine(cluster.url(at_045), poolclass=NullPool)
    try:
        with pytest.raises(DBAPIError) as info:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(_MIGRATION.LOCK_TIMEOUT)
                await connection.exec_driver_sql(_MIGRATION.downgrade_preflight())
    finally:
        await engine.dispose()
    assert _sqlstate(info.value) == "55000"
    assert f"{_DOWNGRADE_REFUSED}alembic_version is {listed}, not 045" in str(
        info.value
    )
    assert await _catalog(cluster, at_045) == before


async def test_the_downgrade_gives_up_after_the_lock_timeout(
    baseline: _Baseline, at_045: str
) -> None:
    """A reader of media_assets holds ACCESS SHARE: the preflight's ACCESS
    EXCLUSIVE lock gives up (55P03) before anything changes."""

    before = await _catalog(baseline.cluster, at_045)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_045, "media_assets", "downgrade", "042"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_045) == before


async def test_the_upgrade_gives_up_after_the_lock_timeout(
    baseline: _Baseline, at_042: str
) -> None:
    """The window's first lock, 043's ACCESS EXCLUSIVE on media_assets for its
    constraint swap, waits for a reader of media_assets, then gives up
    (55P03): one transaction, so nothing of 043, 044 or 045 exists."""

    code, output = await _alembic_while_reading(
        baseline.cluster, at_042, "media_assets", "upgrade", "045"
    )
    assert code is not None, "the migration waited past the bound: no lock timeout"
    assert code != 0, output
    assert "LockNotAvailableError" in output, output
    assert "canceling statement due to lock timeout" in output, output
    statements = re.findall(r"\[SQL:\s*(\S+ \S+)", output)
    assert statements == ["ALTER TABLE"], statements
    assert "DROP CONSTRAINT ck_media_assets_category_mime" in output, output
    assert await _catalog(baseline.cluster, at_042) == baseline.catalog_042


async def _alembic_while_writing(
    cluster: _Cluster, name: str, table: str, command: str, revision: str
) -> tuple[int | None, str]:
    """Run alembic while another session holds a writer's lock on *table*
    (ROW EXCLUSIVE, what an INSERT, UPDATE or DELETE holds until its commit).
    Returns ``(None, output)`` when the run did not finish within the bound."""

    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    connection = await engine.connect()
    process: asyncio.subprocess.Process | None = None
    code: int | None = None
    output = b""
    try:
        await connection.execute(text(f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE"))
        process = await _spawn_alembic(cluster.url(name), command, revision)
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=_LOCK_BOUND_SECONDS
            )
            code = int(process.returncode or 0)
        except asyncio.TimeoutError:
            code = None
    finally:
        await connection.rollback()
        await connection.close()
        await engine.dispose()
    if code is None and process is not None:
        output, _ = await process.communicate()
    return code, output.decode(errors="replace")


@pytest.mark.parametrize("table", ["sessions", "tenants"])
async def test_the_upgrade_gives_up_behind_a_writer_of_a_referenced_table(
    baseline: _Baseline, at_042: str, table: str
) -> None:
    """The foreign keys of media_deliveries take SHARE ROW EXCLUSIVE on the
    tables they reference, which conflicts with a writer's ROW EXCLUSIVE:
    the CREATE TABLE waits, then gives up (55P03), and nothing of 043, 044 or
    045 exists (one transaction: the two constraint swaps and the column
    before it roll back with it; the catalog, the media check included, is
    042's)."""

    code, output = await _alembic_while_writing(
        baseline.cluster, at_042, table, "upgrade", "045"
    )
    assert code is not None, "the migration waited past the bound: no lock timeout"
    assert code != 0, output
    assert "LockNotAvailableError" in output, output
    assert "canceling statement due to lock timeout" in output, output
    statements = re.findall(r"\[SQL:\s*(\S+ \S+)", output)
    assert statements == ["CREATE TABLE"], statements
    assert await _catalog(baseline.cluster, at_042) == baseline.catalog_042


# ---------------------------------------------------------------------------
# Window 045: 043, 044 and 045 in one transaction.
# ---------------------------------------------------------------------------

_MEDIA_CHECK_OID = (
    "SELECT oid FROM pg_catalog.pg_constraint WHERE conrelid = "
    "'public.media_assets'::regclass AND conname = 'ck_media_assets_category_mime'"
)
_LOCKS_OF_THE_WINDOW = """
SELECT lock.pid, relation.relname, lock.mode, lock.granted
FROM pg_catalog.pg_locks AS lock
JOIN pg_catalog.pg_class AS relation ON relation.oid = lock.relation
WHERE lock.database = (
    SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database()
)
AND relation.relname IN ('media_assets', 'tenants', 'sessions')
AND lock.pid <> pg_backend_pid()
"""


@pytest.mark.parametrize("table", ["tenants", "sessions"])
async def test_the_window_is_one_transaction_while_it_waits_for_a_writer(
    baseline: _Baseline, at_042: str, table: str
) -> None:
    """lock_timeout bounds each lock wait, and the run is one transaction:
    while 045's CREATE TABLE waits for a writer of a referenced table,
    nothing of 043 or 044 is committed on its own (another session still
    reads alembic_version 042 and the 042 media check; per-migration
    transactions would show 044 here), media_assets is held ACCESS
    EXCLUSIVE (a reader of media_assets gives up, 55P03), and once the
    writer ends the same run finishes at 045."""

    cluster = baseline.cluster
    writer_engine = create_async_engine(cluster.url(at_042), poolclass=NullPool)
    probe_engine = create_async_engine(
        cluster.url(at_042), poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    writer = await writer_engine.connect()
    process: asyncio.subprocess.Process | None = None
    try:
        async with probe_engine.connect() as probe:
            check_before = (await probe.exec_driver_sql(_MEDIA_CHECK_OID)).scalar()
        await writer.execute(text(f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE"))
        process = await _spawn_alembic(cluster.url(at_042), "upgrade", "045")
        waiting: int | None = None
        held: set[tuple[str, str]] = set()
        committed: list[str] = []
        check_seen: int | None = None
        async with probe_engine.connect() as probe:
            for _attempt in range(int(_LOCK_BOUND_SECONDS / 0.05)):
                rows = (await probe.exec_driver_sql(_LOCKS_OF_THE_WINDOW)).all()
                waiting = next(
                    (
                        pid
                        for pid, relname, mode, granted in rows
                        if relname == table
                        and mode == "ShareRowExclusiveLock"
                        and not granted
                    ),
                    None,
                )
                if waiting is not None or process.returncode is not None:
                    break
                await asyncio.sleep(0.05)
            assert waiting is not None, "the window never waited for the writer"
            held = {
                (relname, mode)
                for pid, relname, mode, granted in rows
                if pid == waiting and granted
            }
            # What another session sees committed while the run waits: plain
            # MVCC reads of alembic_version and of the constraint's catalog
            # row (pg_get_constraintdef would wait for media_assets' lock).
            committed = list(
                (
                    await probe.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    )
                ).scalars()
            )
            check_seen = (await probe.exec_driver_sql(_MEDIA_CHECK_OID)).scalar()
        reader_engine = create_async_engine(cluster.url(at_042), poolclass=NullPool)
        try:
            with pytest.raises(DBAPIError) as info:
                async with reader_engine.begin() as reader:
                    await reader.exec_driver_sql("SET LOCAL lock_timeout = '200ms'")
                    await reader.exec_driver_sql("SELECT count(*) FROM media_assets")
        finally:
            await reader_engine.dispose()
    finally:
        await writer.rollback()
        await writer.close()
        await writer_engine.dispose()
        await probe_engine.dispose()
    # The 042 constraint row, not one re-created by a committed 043 or 044.
    assert committed == ["042"], committed
    assert check_seen is not None and check_seen == check_before
    assert ("media_assets", "AccessExclusiveLock") in held, held
    if table == "sessions":
        # tenants came first, and its lock is held too.
        assert ("tenants", "ShareRowExclusiveLock") in held, held
    assert _sqlstate(info.value) == "55P03"
    assert process is not None
    output, _ = await asyncio.wait_for(
        process.communicate(), timeout=_LOCK_BOUND_SECONDS
    )
    assert process.returncode == 0, output.decode(errors="replace")
    assert (await _catalog(cluster, at_042))["alembic_version"] == [("045",)]


_INSERT_AUDIO = (
    "INSERT INTO media_assets (id, tenant_id, display_name, original_filename, "
    "category, mime_type, size_bytes, sha256, payload) VALUES (:id, :tenant, "
    "'Audio', 'audio.bin', 'audio', :mime, 4, :sha256, :payload)"
)


@pytest.mark.parametrize(
    "mime,downgrades",
    [
        ("audio/mp4", ["045 -> 044", "044 -> 043"]),
        ("audio/ogg", ["045 -> 044", "044 -> 043", "043 -> 042"]),
    ],
)
async def test_an_ogg_or_mp4_asset_fails_the_window_downgrade_with_045_intact(
    baseline: _Baseline, at_045: str, mime: str, downgrades: list[str]
) -> None:
    """With no delivery and no intent, 045's own downgrade passes, but the
    constraint swap of 044 (audio/mp4) or of 043 (audio/ogg) back to the
    narrower invariant fails (23514), and the one transaction keeps 045."""

    cluster = baseline.cluster
    await _execute(
        cluster,
        at_045,
        _INSERT_AUDIO,
        id=UUID("00000000-0000-4000-8000-000000004325"),
        tenant=_TENANT,
        mime=mime,
        sha256=b"\x03" * 32,
        payload=b"\x00\x00\x00\x00",
    )
    before = await _catalog(cluster, at_045)
    code, output = await _alembic(cluster.url(at_045), "downgrade", "042")
    assert code != 0, output
    assert "CheckViolationError" in output, output  # SQLSTATE 23514
    assert (
        'check constraint "ck_media_assets_category_mime" of relation'
        ' "media_assets" is violated by some row' in output
    ), output
    assert re.findall(r"Running downgrade (\d{3} -> \d{3})", output) == downgrades
    assert await _catalog(cluster, at_045) == before
    assert before["alembic_version"] == [("045",)]


_PREFLIGHT_SQL = (_ROOT / "scripts" / "migration_043_045_preflight.sql").read_text(
    encoding="utf-8"
)
_INVARIANT_042 = (
    "(category = 'video' AND mime_type = 'video/mp4') OR "
    "(category = 'audio' AND mime_type = 'audio/mpeg') OR "
    "(category = 'pdf' AND mime_type = 'application/pdf') OR "
    "(category = 'image' AND mime_type IN ('image/jpeg', 'image/png'))"
)
_DROP_CHECK = "ALTER TABLE media_assets DROP CONSTRAINT ck_media_assets_category_mime"


@pytest.mark.parametrize(
    "upgrade,statements,counts",
    [
        (None, (), (0, 0, 0)),
        # The 042 invariant re-created from its source: still 042's.
        (
            None,
            (
                _DROP_CHECK,
                "ALTER TABLE media_assets ADD CONSTRAINT "
                f"ck_media_assets_category_mime CHECK ({_INVARIANT_042})",
            ),
            (0, 0, 0),
        ),
        # Drifted: already widened by hand, the revision still 042.
        (
            None,
            (
                _DROP_CHECK,
                "ALTER TABLE media_assets ADD CONSTRAINT "
                "ck_media_assets_category_mime CHECK ("
                + _INVARIANT_042.replace(
                    "mime_type = 'audio/mpeg'",
                    "mime_type IN ('audio/mpeg', 'audio/ogg')",
                )
                + ")",
            ),
            (0, 0, 1),
        ),
        (None, (_DROP_CHECK,), (0, 0, 1)),
        (
            None,
            (
                _DROP_CHECK,
                "ALTER TABLE media_assets ADD CONSTRAINT "
                f"ck_media_assets_category_mime CHECK ({_INVARIANT_042}) NOT VALID",
            ),
            (0, 0, 1),
        ),
        ("044", (), (0, 0, 1)),
        ("045", (), (1, 1, 1)),
    ],
    ids=[
        "at-042",
        "042-recreated",
        "drifted",
        "missing",
        "not-valid",
        "at-044",
        "at-045",
    ],
)
async def test_the_window_preflight_reads_zero_only_at_the_042_catalog(
    baseline: _Baseline,
    at_042: str,
    upgrade: str | None,
    statements: tuple[str, ...],
    counts: tuple[int, int, int],
) -> None:
    cluster = baseline.cluster
    if upgrade is not None:
        await _migrate(cluster.url(at_042), "upgrade", upgrade)
    if statements:
        await _execute(cluster, at_042, *statements)
    engine = create_async_engine(cluster.url(at_042), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (await connection.exec_driver_sql(_PREFLIGHT_SQL)).all()
    finally:
        await engine.dispose()
    assert [row[0] for row in rows] == [
        "045 tables present",
        "045 columns present",
        "media mime check not at 042",
    ]
    assert tuple(row[1] for row in rows) == counts
