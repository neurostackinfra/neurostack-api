"""Migration 046, the courtesy credit bridge release, on real PostgreSQL.

creditos-cortesia Group 1.  PostgreSQL only.  A module template at ``045``
with credit rows that existed before the bridge (two builders' accounts, a
monthly and a top-up lot with their grants), and a second template upgraded
from it to ``046`` as window 046 does; every test clones one of them.
Pinned here:

* the upgrade keeps every 045 row and widens only: ``ck_credit_lots_kind``
  and ``ck_credit_lots_source`` admit a courtesy lot (no subscription,
  order, cycle or payment; ``grant_effect_id`` ``courtesy:{event_id}:grant``)
  and refuse it with any of those (SQLSTATE 23514, by name); a monthly or a
  top-up lot without ``payment_id`` is refused too (the NOT NULL became a
  per-kind CHECK); ``paid_at`` stays NOT NULL;
* ``ck_credit_ledger_entries_type`` admits ``courtesy_removal`` with a
  ``courtesy-removal:`` effect id only, and it names a lot;
* the table ``credit_courtesy_events``: its columns and constraints exactly,
  each CHECK, the idempotency key unique across builders (23505), the lot
  referenced inside one builder (23503), and the append-only trigger
  (UPDATE and DELETE refused with the 038 message, SQLSTATE 23000);
* the downgrade: an empty round trip restores the 045 catalog exactly, and
  a courtesy lot, a ``courtesy_removal`` entry or an event refuses it with
  SQLSTATE 55000 and one message, changing nothing; another revision is
  refused too; both directions give up after the lock timeout, and the
  upgrade is one transaction (giving up on the ledger leaves the lots'
  CHECKs at 045);
* the window's preflight SQL reads every count 0 only at the 045 catalog,
  and its post-read reads NULL at 045 and true at 046.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

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
    _alembic,
    _alembic_while_reading,
    _assert_lock_timeout_in_the_preflight,
    _catalog,
    _Cluster,
    _execute,
    _migrate,
    _refusal,
    _scalar,
)

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION_PATH = (
    _ROOT / "alembic" / "versions" / "20261109_000001_046_credit_courtesy.py"
)
_DOWNGRADE_REFUSED = "downgrade of migration 046 refused (nothing was changed): "
# The guard messages (046's downgrade refusal and 038's append-only trigger
# function), hard-coded literals: each probe pins its refusal by the SQLSTATE
# and this exact runtime message, so a refusal is pinned to its own guard.
_APPEND_ONLY = "credit_courtesy_events is append-only: correct it with a new row"

_BUILDER = UUID("00000000-0000-4000-8000-000000004601")
_OTHER_BUILDER = UUID("00000000-0000-4000-8000-000000004602")
_ACCOUNT = UUID("00000000-0000-4000-8000-000000004611")
_OTHER_ACCOUNT = UUID("00000000-0000-4000-8000-000000004612")
_MONTHLY_LOT = UUID("00000000-0000-4000-8000-000000004621")
_TOPUP_LOT = UUID("00000000-0000-4000-8000-000000004622")
_OTHER_LOT = UUID("00000000-0000-4000-8000-000000004623")
_ACTOR = UUID("00000000-0000-4000-8000-000000004631")
_PAID = date(2026, 11, 9)
_AT = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
_EXPIRES = datetime(2026, 12, 9, 3, 0, tzinfo=timezone.utc)
_DIGEST = "a" * 64


def _load_migration() -> ModuleType:
    """The migration module itself (its preflight SQL, run directly).

    In-process, the repository's ``alembic/`` directory may shadow the
    package; ``op`` is only needed as a name for the module to import.
    """

    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_046", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MIGRATION = _load_migration()

_INSERT_ACCOUNT = "INSERT INTO credit_accounts (id, builder_id) VALUES (:id, :builder)"
_INSERT_EFFECT = (
    "INSERT INTO credit_effects (id, builder_id, effect_id, effect_kind) "
    "VALUES (gen_random_uuid(), :builder, :effect, 'test')"
)
_INSERT_LOT = (
    "INSERT INTO credit_lots (id, builder_id, account_id, lot_kind, "
    "subscription_id, cycle_start, cycle_end, order_id, payment_id, paid_at, "
    "granted_mc, expires_at, grant_effect_id) VALUES (:id, :builder, :account, "
    ":kind, :subscription, :cycle_start, :cycle_end, :order_id, :payment, "
    ":paid_at, :granted, :expires, :effect)"
)
_INSERT_ENTRY = (
    "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
    "lot_id, entry_type, amount_mc, occurred_at) VALUES (gen_random_uuid(), "
    ":builder, :account, :effect, :lot, :entry_type, :amount, :at)"
)
_INSERT_EVENT = (
    "INSERT INTO credit_courtesy_events (id, builder_id, lot_id, event_kind, "
    "amount_mc, reason, actor_user_id, idempotency_key, request_digest, "
    "occurred_at) VALUES (:id, :builder, :lot, :kind, :amount, :reason, "
    ":actor, :key, :digest, :at)"
)


def _lot(**changes: Any) -> dict[str, Any]:
    """A valid courtesy lot of _BUILDER, with its grant effect's id."""

    row: dict[str, Any] = {
        "id": uuid4(),
        "builder": _BUILDER,
        "account": _ACCOUNT,
        "kind": "courtesy",
        "subscription": None,
        "cycle_start": None,
        "cycle_end": None,
        "order_id": None,
        "payment": None,
        "paid_at": _PAID,
        "granted": 10_000_000,
        "expires": _EXPIRES,
        "effect": f"courtesy:{uuid4()}:grant",
    }
    row.update(changes)
    return row


def _event(lot: UUID, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4(),
        "builder": _BUILDER,
        "lot": lot,
        "kind": "grant",
        "amount": 10_000_000,
        "reason": "conta de teste",
        "actor": _ACTOR,
        "key": f"key-{uuid4()}",
        "digest": _DIGEST,
        "at": _AT,
    }
    row.update(changes)
    return row


async def _insert_lot(cluster: _Cluster, name: str, row: dict[str, Any]) -> None:
    """The lot and its grant effect, in one transaction."""

    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(_INSERT_EFFECT),
                {"builder": row["builder"], "effect": row["effect"]},
            )
            await connection.execute(text(_INSERT_LOT), row)
    finally:
        await engine.dispose()


async def _insert_entry(
    cluster: _Cluster, name: str, *, lot: UUID | None, entry_type: str, effect: str
) -> None:
    engine = create_async_engine(cluster.url(name), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(_INSERT_EFFECT), {"builder": _BUILDER, "effect": effect}
            )
            await connection.execute(
                text(_INSERT_ENTRY),
                {
                    "builder": _BUILDER,
                    "account": _ACCOUNT,
                    "effect": effect,
                    "lot": lot,
                    "entry_type": entry_type,
                    "amount": 1_000,
                    "at": _AT,
                },
            )
    finally:
        await engine.dispose()


def _refused(info: pytest.ExceptionInfo[DBAPIError], sqlstate: str, guard: str) -> None:
    assert _sqlstate(info.value) == sqlstate, info.value
    assert guard in str(info.value), info.value


# ---------------------------------------------------------------------------
# Module fixture: a 045 template with the credit rows that existed before 046,
# and a 046 template upgraded from it as window 046 does.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Baseline:
    cluster: _Cluster
    template_045: str
    template_046: str
    catalog_045: dict[str, Any]


async def _seed(cluster: _Cluster, name: str) -> None:
    for account, builder in ((_ACCOUNT, _BUILDER), (_OTHER_ACCOUNT, _OTHER_BUILDER)):
        await _execute(cluster, name, _INSERT_ACCOUNT, id=account, builder=builder)
    await _insert_lot(
        cluster,
        name,
        _lot(
            id=_MONTHLY_LOT,
            kind="monthly",
            subscription=uuid4(),
            cycle_start=date(2026, 11, 1),
            cycle_end=date(2026, 12, 1),
            payment="pay_monthly",
            granted=5_000_000,
            effect=f"grant:monthly:{uuid4()}",
        ),
    )
    await _insert_lot(
        cluster,
        name,
        _lot(
            id=_TOPUP_LOT,
            kind="topup",
            order_id=uuid4(),
            payment="pay_topup",
            granted=500_000,
            effect=f"grant:topup:{uuid4()}",
        ),
    )
    await _insert_lot(
        cluster,
        name,
        _lot(
            id=_OTHER_LOT,
            builder=_OTHER_BUILDER,
            account=_OTHER_ACCOUNT,
            kind="topup",
            order_id=uuid4(),
            payment="pay_other",
            effect=f"grant:topup:{uuid4()}",
        ),
    )
    for lot, amount in ((_MONTHLY_LOT, 5_000_000), (_TOPUP_LOT, 500_000)):
        effect = await _scalar(
            cluster,
            name,
            "SELECT grant_effect_id FROM credit_lots WHERE id = :id",
            id=lot,
        )
        await _execute(
            cluster,
            name,
            _INSERT_ENTRY,
            builder=_BUILDER,
            account=_ACCOUNT,
            effect=effect,
            lot=lot,
            entry_type="grant",
            amount=amount,
            at=_AT,
        )


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
        template_045 = await cluster.fresh()
        await _migrate(cluster.url(template_045), "upgrade", "045")
        await _seed(cluster, template_045)
        catalog_045 = await _catalog(cluster, template_045)
        template_046 = await cluster.clone(template_045)
        await _migrate(cluster.url(template_046), "upgrade", "046")
        yield _Baseline(cluster, template_045, template_046, catalog_045)
    finally:
        for name in list(reversed(cluster.created)):
            await cluster.drop(name)
        await admin.dispose()


@pytest.fixture
async def at_045(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_045)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


@pytest.fixture
async def at_046(baseline: _Baseline) -> AsyncIterator[str]:
    name = await baseline.cluster.clone(baseline.template_046)
    try:
        yield name
    finally:
        await baseline.cluster.drop(name)


# ---------------------------------------------------------------------------
# The revision and the ORM (no database).
# ---------------------------------------------------------------------------


def test_046_revises_045_and_only_047_follows_it() -> None:
    assert (_MIGRATION.revision, _MIGRATION.down_revision) == ("046", "045")
    revisions = migration_revisions()
    assert revisions[_MIGRATION_PATH.name] == ("046", "045")
    # cobranca-credito-por-builder: 047 (enforcement marks) is the one child
    # of 046 and the head.
    assert heads(revisions) == ["047"]
    assert [rev for rev, down in revisions.values() if down == "046"] == ["047"]
    # A credit migration that deliberately keeps CREDIT_SCHEMA_MIN_REVISION
    # at 042 (tests/services/test_credit_switch.py names it).
    assert "_credit_" in _MIGRATION_PATH.name
    assert _MIGRATION.CREATED_TABLES == ("credit_courtesy_events",)
    assert _MIGRATION.REWRITTEN_CHECKS == (
        ("credit_lots", "ck_credit_lots_kind"),
        ("credit_lots", "ck_credit_lots_source"),
        ("credit_ledger_entries", "ck_credit_ledger_entries_type"),
    )
    assert _MIGRATION.RELAXED_COLUMNS == (("credit_lots", "payment_id"),)
    assert _MIGRATION.LOCK_TIMEOUT == "SET LOCAL lock_timeout = '5s'"


# The files the courtesy release (creditos-cortesia Decision 17) may name the
# courtesy schema in; empty in the bridge release (Decision 4).
_COURTESY_FILES = frozenset(
    {
        "src/api/routes/admin.py",
        "src/api/routes/admin_credits.py",
        "src/api/routes/credits.py",
        "src/api/schemas/admin_credits.py",
        "src/api/schemas/credits.py",
        "src/services/credit_courtesy.py",
        "src/services/credit_ledger.py",
        "src/services/credit_lifecycle.py",
        "src/services/credit_rollout.py",
    }
)
# The files that name "courtesy" only as the ENFORCEMENT ORIGIN of the
# courtesy rule (debito-cortesia-automatico: ``effective_enforcement`` returns
# ``origin="courtesy"``), never the courtesy schema nor the lot kind (both
# still forbidden in them, below).
_COURTESY_RULE_FILES = frozenset(
    {
        # ``_enforced_book`` skips the ASAAS gates when the captured origin is
        # the rule (the reservation is paid from courtesy only).
        "src/services/credit_inventory.py",
        # The prechecks, the capture and the debit phases compare the
        # captured origin (``mark`` vs ``courtesy``) and build the rule's
        # captured mode.
        "src/services/credit_metering.py",
        # Uazapi/Evolution agent media are charged only under the rule
        # (origin ``courtesy``).
        "src/services/reply_media.py",
    }
)
# What a courtesy-rule file must still never name: the courtesy schema...
_COURTESY_SCHEMA = re.compile(
    r"credit_courtesy_events|CreditCourtesyEvent|courtesy_removal"
)
# ...nor the courtesy LOT KIND (a ``lot_kind`` compared with "courtesy" on
# one line, either order).
_COURTESY_LOT_KIND = re.compile(
    r"lot_kind[^\n]*['\"]courtesy['\"]|['\"]courtesy['\"][^\n]*lot_kind"
)


def test_the_bridge_maps_the_046_schema_and_nothing_writes_or_reads_it() -> None:
    """creditos-cortesia Decision 4: the bridge image's ORM describes 046 (the
    drift suite compares it with this DDL) and no service or route names the
    courtesy lot kind, the courtesy_removal entry or the event table, except
    the files in ``_COURTESY_FILES``.  In the bridge release (PR-A) that
    allowlist is empty by design; the courtesy release (Decision 17) extends
    it only with the files that implement the feature, and each of them must
    exist, so a rename cannot silently weaken the guard.

    debito-cortesia-automatico adds a second, separate list,
    ``_COURTESY_RULE_FILES``: files that may name "courtesy" only as the
    courtesy rule's enforcement origin.  They must exist too, and they must
    still name neither the courtesy schema (``_COURTESY_SCHEMA``) nor the
    courtesy lot kind (``_COURTESY_LOT_KIND``)."""

    import src.models  # noqa: F401  (registers every model on Base.metadata)
    from src.models.credit import LEDGER_ENTRY_TYPES, LOT_KINDS

    assert "credit_courtesy_events" in Base.metadata.tables
    assert Base.metadata.tables["credit_lots"].columns["payment_id"].nullable
    assert LOT_KINDS[-1] == "courtesy" and LEDGER_ENTRY_TYPES[-1] == "courtesy_removal"
    pattern = re.compile(
        r"credit_courtesy_events|CreditCourtesyEvent|courtesy_removal"
        r"|'courtesy'|\"courtesy\""
    )
    # PR-A's staged version had no allowlist: every file was scanned.
    assert not _COURTESY_FILES & _COURTESY_RULE_FILES
    for allowed in _COURTESY_FILES | _COURTESY_RULE_FILES:
        assert (_ROOT / allowed).is_file(), allowed
    for root in ("src/services", "src/api"):
        for path in sorted((_ROOT / root).rglob("*.py")):
            relative = path.relative_to(_ROOT).as_posix()
            if relative in _COURTESY_FILES:
                continue
            source = path.read_text(encoding="utf-8")
            if relative in _COURTESY_RULE_FILES:
                assert _COURTESY_SCHEMA.search(source) is None, path
                assert _COURTESY_LOT_KIND.search(source) is None, path
                continue
            assert pattern.search(source) is None, path


# ---------------------------------------------------------------------------
# The upgrade: the 045 rows, the widened CHECKs and the nullable payment_id.
# ---------------------------------------------------------------------------


async def test_rows_that_existed_at_045_survive_unchanged(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    engine = create_async_engine(cluster.url(at_046), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            lots = (
                await connection.execute(
                    text(
                        "SELECT id, lot_kind, payment_id, granted_mc FROM credit_lots "
                        "ORDER BY id"
                    )
                )
            ).all()
            column = (
                await connection.execute(
                    text(
                        "SELECT a.attnotnull FROM pg_attribute AS a WHERE a.attrelid "
                        "= 'public.credit_lots'::regclass AND a.attname = :name"
                    ),
                    {"name": "payment_id"},
                )
            ).scalar()
            paid_at = (
                await connection.execute(
                    text(
                        "SELECT a.attnotnull FROM pg_attribute AS a WHERE a.attrelid "
                        "= 'public.credit_lots'::regclass AND a.attname = :name"
                    ),
                    {"name": "paid_at"},
                )
            ).scalar()
            entries = await connection.scalar(
                text("SELECT count(*) FROM credit_ledger_entries")
            )
    finally:
        await engine.dispose()
    assert [tuple(row) for row in lots] == [
        (_MONTHLY_LOT, "monthly", "pay_monthly", 5_000_000),
        (_TOPUP_LOT, "topup", "pay_topup", 500_000),
        (_OTHER_LOT, "topup", "pay_other", 10_000_000),
    ]
    assert column is False and paid_at is True
    assert entries == 2
    assert (await _catalog(cluster, at_046))["alembic_version"] == [("046",)]


async def test_a_courtesy_lot_with_no_source_and_no_payment_is_admitted(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    lot = _lot()
    await _insert_lot(cluster, at_046, lot)
    assert (
        await _scalar(
            cluster,
            at_046,
            "SELECT payment_id IS NULL AND paid_at = :paid FROM credit_lots "
            "WHERE id = :id",
            id=lot["id"],
            paid=_PAID,
        )
        is True
    )


@pytest.mark.parametrize(
    "changes,guard",
    [
        # Each differs from an admitted row in one field only.
        ({"subscription": UUID(int=1)}, "ck_credit_lots_source"),
        ({"order_id": UUID(int=2)}, "ck_credit_lots_source"),
        ({"cycle_start": date(2026, 11, 1)}, "ck_credit_lots_source"),
        ({"cycle_end": date(2026, 12, 1)}, "ck_credit_lots_source"),
        ({"payment": "pay_courtesy"}, "ck_credit_lots_source"),
        ({"effect": f"grant:{UUID(int=3)}"}, "ck_credit_lots_source"),
        ({"effect": f"courtesy:{UUID(int=4)}:removal"}, "ck_credit_lots_source"),
        ({"effect": f"courtesy-removal:{UUID(int=5)}"}, "ck_credit_lots_source"),
        # A kind no migration admits fails the kind CHECK first (PostgreSQL
        # tests CHECKs in name order: granted_mc, kind, source).
        ({"kind": "bonus"}, "ck_credit_lots_kind"),
        ({"granted": 0}, "ck_credit_lots_granted_mc"),
    ],
    ids=[
        "subscription",
        "order",
        "cycle-start",
        "cycle-end",
        "payment",
        "grant-effect",
        "removal-suffix",
        "removal-effect",
        "unknown-kind",
        "zero-grant",
    ],
)
async def test_a_courtesy_lot_with_any_source_or_payment_is_refused(
    baseline: _Baseline, at_046: str, changes: dict[str, Any], guard: str
) -> None:
    with pytest.raises(DBAPIError, match=guard) as info:
        await _insert_lot(baseline.cluster, at_046, _lot(**changes))
    _refused(info, "23514", guard)
    assert (
        await _scalar(
            baseline.cluster,
            at_046,
            "SELECT count(*) FROM credit_lots WHERE lot_kind <> 'monthly' "
            "AND lot_kind <> 'topup'",
        )
        == 0
    )


@pytest.mark.parametrize(
    "kind,changes",
    [
        (
            "monthly",
            {
                "subscription": UUID(int=11),
                "cycle_start": date(2026, 12, 1),
                "cycle_end": date(2027, 1, 1),
            },
        ),
        ("topup", {"order_id": UUID(int=12)}),
    ],
)
async def test_a_paid_lot_without_its_payment_is_refused(
    baseline: _Baseline, at_046: str, kind: str, changes: dict[str, Any]
) -> None:
    """The NOT NULL of 045 became part of ck_credit_lots_source: monthly and
    top-up lots still need their payment, and with one they are admitted."""

    cluster = baseline.cluster
    effect = f"grant:{kind}:{uuid4()}"
    with pytest.raises(DBAPIError, match="ck_credit_lots_source") as info:
        await _insert_lot(
            cluster, at_046, _lot(kind=kind, payment=None, effect=effect, **changes)
        )
    _refused(info, "23514", "ck_credit_lots_source")
    await _insert_lot(
        cluster, at_046, _lot(kind=kind, payment="pay_1", effect=effect, **changes)
    )


async def test_the_lot_paid_at_is_still_required(
    baseline: _Baseline, at_046: str
) -> None:
    with pytest.raises(DBAPIError, match="paid_at") as info:
        await _insert_lot(baseline.cluster, at_046, _lot(paid_at=None))
    assert _sqlstate(info.value) == "23502"


async def test_a_courtesy_removal_names_a_lot_and_its_own_effect_id(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    lot = _lot()
    await _insert_lot(cluster, at_046, lot)
    await _insert_entry(
        cluster,
        at_046,
        lot=lot["id"],
        entry_type="courtesy_removal",
        effect=f"courtesy-removal:{uuid4()}",
    )
    with pytest.raises(DBAPIError, match="ck_credit_ledger_entries_type") as info:
        await _insert_entry(
            cluster,
            at_046,
            lot=lot["id"],
            entry_type="courtesy_removal",
            effect=f"removal:{uuid4()}",
        )
    _refused(info, "23514", "ck_credit_ledger_entries_type")
    with pytest.raises(DBAPIError, match="ck_credit_ledger_entries_lot") as info:
        await _insert_entry(
            cluster,
            at_046,
            lot=None,
            entry_type="courtesy_removal",
            effect=f"courtesy-removal:{uuid4()}",
        )
    _refused(info, "23514", "ck_credit_ledger_entries_lot")
    # Another entry type in that namespace is not pinned by the CHECK.
    await _insert_entry(
        cluster,
        at_046,
        lot=lot["id"],
        entry_type="debit",
        effect=f"courtesy-removal:{uuid4()}",
    )
    assert (
        await _scalar(
            cluster,
            at_046,
            "SELECT count(*) FROM credit_ledger_entries "
            "WHERE entry_type = 'courtesy_removal'",
        )
        == 1
    )


# ---------------------------------------------------------------------------
# The event table.
# ---------------------------------------------------------------------------


async def test_credit_courtesy_events_is_exactly_the_046_table(
    baseline: _Baseline, at_046: str
) -> None:
    catalog = await _catalog(baseline.cluster, at_046)
    columns = [
        row[1:5] for row in catalog["columns"] if row[0] == "credit_courtesy_events"
    ]
    assert sorted(columns) == sorted(
        [
            ("id", "uuid", True, ""),
            ("builder_id", "uuid", True, ""),
            ("lot_id", "uuid", True, ""),
            ("event_kind", "character varying(20)", True, ""),
            ("amount_mc", "bigint", True, ""),
            ("reason", "character varying(500)", True, ""),
            ("actor_user_id", "uuid", True, ""),
            ("idempotency_key", "character varying(200)", True, ""),
            ("request_digest", "character varying(64)", True, ""),
            ("occurred_at", "timestamp with time zone", True, ""),
            ("created_at", "timestamp with time zone", True, "now()"),
        ]
    )
    constraints = {
        row[1]: (row[2], row[3])
        for row in catalog["constraints"]
        if row[0] == "credit_courtesy_events"
    }
    assert constraints == {
        "credit_courtesy_events_pkey": ("p", "PRIMARY KEY (id)"),
        "ck_credit_courtesy_events_kind": (
            "c",
            "CHECK (((event_kind)::text = ANY ((ARRAY['grant'::character varying, "
            "'removal'::character varying])::text[])))",
        ),
        "ck_credit_courtesy_events_amount": ("c", "CHECK ((amount_mc > 0))"),
        "ck_credit_courtesy_events_reason": (
            "c",
            "CHECK ((length((reason)::text) > 0))",
        ),
        "ck_credit_courtesy_events_idempotency_key": (
            "c",
            "CHECK ((length((idempotency_key)::text) > 0))",
        ),
        "ck_credit_courtesy_events_request_digest": (
            "c",
            "CHECK (((request_digest)::text ~ '^[0-9a-f]{64}$'::text))",
        ),
        "fk_credit_courtesy_events_lot": (
            "f",
            "FOREIGN KEY (builder_id, lot_id) REFERENCES credit_lots(builder_id, id)"
            " ON DELETE RESTRICT",
        ),
        "uq_credit_courtesy_events_idempotency_key": ("u", "UNIQUE (idempotency_key)"),
    }
    triggers = [
        row[1:3] for row in catalog["triggers"] if row[0] == "credit_courtesy_events"
    ]
    assert triggers == [
        (
            "trg_credit_courtesy_events_immutable",
            "CREATE TRIGGER trg_credit_courtesy_events_immutable BEFORE DELETE OR "
            "UPDATE ON public.credit_courtesy_events FOR EACH ROW EXECUTE FUNCTION "
            "reject_credit_history_mutation()",
        )
    ]
    assert (
        await _scalar(
            baseline.cluster,
            at_046,
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND indexname = 'ix_credit_courtesy_events_builder_lot'",
        )
        == "CREATE INDEX ix_credit_courtesy_events_builder_lot ON "
        "public.credit_courtesy_events USING btree (builder_id, lot_id)"
    )


async def test_a_grant_and_a_removal_event_are_recorded(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    lot = _lot()
    await _insert_lot(cluster, at_046, lot)
    for row in (
        _event(lot["id"]),
        _event(lot["id"], kind="removal", amount=6_000_000),
        # remove_all of a fractional balance: any positive millicredits.
        _event(lot["id"], kind="removal", amount=400),
    ):
        await _execute(cluster, at_046, _INSERT_EVENT, **row)
    assert (
        await _scalar(
            cluster,
            at_046,
            "SELECT count(*) FROM credit_courtesy_events WHERE lot_id = :lot "
            "AND created_at IS NOT NULL",
            lot=lot["id"],
        )
        == 3
    )


@pytest.mark.parametrize(
    "changes,guard",
    [
        ({"amount": 0}, "ck_credit_courtesy_events_amount"),
        ({"amount": -1_000}, "ck_credit_courtesy_events_amount"),
        ({"key": ""}, "ck_credit_courtesy_events_idempotency_key"),
        ({"kind": "expiry"}, "ck_credit_courtesy_events_kind"),
        ({"reason": ""}, "ck_credit_courtesy_events_reason"),
        ({"digest": "A" * 64}, "ck_credit_courtesy_events_request_digest"),
        ({"digest": "a" * 63}, "ck_credit_courtesy_events_request_digest"),
    ],
    ids=[
        "zero-amount",
        "negative-amount",
        "empty-key",
        "unknown-kind",
        "empty-reason",
        "upper-digest",
        "short-digest",
    ],
)
async def test_each_event_check_refuses_its_own_field(
    baseline: _Baseline, at_046: str, changes: dict[str, Any], guard: str
) -> None:
    cluster = baseline.cluster
    lot = _lot()
    await _insert_lot(cluster, at_046, lot)
    with pytest.raises(DBAPIError, match=guard) as info:
        await _execute(cluster, at_046, _INSERT_EVENT, **_event(lot["id"], **changes))
    _refused(info, "23514", guard)
    assert (
        await _scalar(cluster, at_046, "SELECT count(*) FROM credit_courtesy_events")
        == 0
    )


async def test_the_event_text_bounds_are_the_api_bounds(
    baseline: _Baseline, at_046: str
) -> None:
    """A reason of 500 characters and a key of 200 are stored; one more
    character is refused (22001)."""

    cluster = baseline.cluster
    lot = _lot()
    await _insert_lot(cluster, at_046, lot)
    await _execute(
        cluster,
        at_046,
        _INSERT_EVENT,
        **_event(lot["id"], reason="r" * 500, key="k" * 200),
    )
    for changes in ({"reason": "r" * 501}, {"key": "x" * 201}):
        with pytest.raises(DBAPIError) as info:
            await _execute(
                cluster, at_046, _INSERT_EVENT, **_event(lot["id"], **changes)
            )
        assert _sqlstate(info.value) == "22001", changes


async def test_the_idempotency_key_is_unique_across_builders(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    lot = _lot()
    other = _lot(builder=_OTHER_BUILDER, account=_OTHER_ACCOUNT)
    await _insert_lot(cluster, at_046, lot)
    await _insert_lot(cluster, at_046, other)
    await _execute(cluster, at_046, _INSERT_EVENT, **_event(lot["id"], key="k-1"))
    with pytest.raises(
        DBAPIError, match="uq_credit_courtesy_events_idempotency_key"
    ) as info:
        await _execute(
            cluster,
            at_046,
            _INSERT_EVENT,
            **_event(other["id"], builder=_OTHER_BUILDER, key="k-1"),
        )
    _refused(info, "23505", "uq_credit_courtesy_events_idempotency_key")


async def test_an_event_names_a_lot_of_its_own_builder(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    with pytest.raises(DBAPIError, match="fk_credit_courtesy_events_lot") as info:
        # _OTHER_LOT exists, but belongs to _OTHER_BUILDER.
        await _execute(cluster, at_046, _INSERT_EVENT, **_event(_OTHER_LOT))
    _refused(info, "23503", "fk_credit_courtesy_events_lot")
    with pytest.raises(DBAPIError, match="fk_credit_courtesy_events_lot") as info:
        await _execute(cluster, at_046, _INSERT_EVENT, **_event(uuid4()))
    _refused(info, "23503", "fk_credit_courtesy_events_lot")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE credit_courtesy_events SET reason = 'outro motivo'",
        "UPDATE credit_courtesy_events SET amount_mc = amount_mc",
        "DELETE FROM credit_courtesy_events",
    ],
    ids=["update-reason", "update-same", "delete"],
)
async def test_events_are_append_only(
    baseline: _Baseline, at_046: str, statement: str
) -> None:
    cluster = baseline.cluster
    lot = _lot()
    await _insert_lot(cluster, at_046, lot)
    row = _event(lot["id"])
    await _execute(cluster, at_046, _INSERT_EVENT, **row)
    with pytest.raises(DBAPIError, match=_APPEND_ONLY) as info:
        await _execute(cluster, at_046, statement)
    _refused(info, "23000", _APPEND_ONLY)
    assert (
        await _scalar(
            cluster,
            at_046,
            "SELECT reason FROM credit_courtesy_events WHERE id = :id",
            id=row["id"],
        )
        == "conta de teste"
    )


# ---------------------------------------------------------------------------
# The downgrade.
# ---------------------------------------------------------------------------


async def test_the_empty_round_trip_restores_045_exactly(
    baseline: _Baseline, at_046: str
) -> None:
    """045 -> 046 (the template, the window's run) -> 045 -> 046 with no
    courtesy row: the downgrade restores the 045 catalog exactly (the CHECKs,
    payment_id NOT NULL, no event table, column order included)."""

    cluster = baseline.cluster
    await _migrate(cluster.url(at_046), "downgrade", "045")
    assert await _catalog(cluster, at_046) == baseline.catalog_045
    await _migrate(cluster.url(at_046), "upgrade", "046")
    assert (await _catalog(cluster, at_046))["alembic_version"] == [("046",)]


async def _plant_lot(cluster: _Cluster, name: str) -> UUID:
    lot = _lot()
    await _insert_lot(cluster, name, lot)
    return UUID(str(lot["id"]))


async def _plant_removal(cluster: _Cluster, name: str) -> None:
    # On the monthly lot: the CHECK pins the effect id, not the lot's kind.
    await _insert_entry(
        cluster,
        name,
        lot=_MONTHLY_LOT,
        entry_type="courtesy_removal",
        effect=f"courtesy-removal:{uuid4()}",
    )


async def _plant_event(cluster: _Cluster, name: str) -> None:
    # On the top-up lot: the foreign key names a lot, not its kind.
    await _execute(cluster, name, _INSERT_EVENT, **_event(_TOPUP_LOT))


_PLANTERS = {
    "courtesy lots": _plant_lot,
    "courtesy_removal ledger entries": _plant_removal,
    "credit_courtesy_events rows": _plant_event,
}


@pytest.mark.parametrize("kind", sorted(_PLANTERS))
async def test_any_courtesy_row_refuses_the_downgrade(
    baseline: _Baseline, at_046: str, kind: str
) -> None:
    cluster = baseline.cluster
    await _PLANTERS[kind](cluster, at_046)
    before = await _catalog(cluster, at_046)
    code, output = await _alembic(cluster.url(at_046), "downgrade", "045")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        f"{_DOWNGRADE_REFUSED}courtesy credit exists: {kind}"
    )
    assert await _catalog(cluster, at_046) == before
    assert before["alembic_version"] == [("046",)]


async def test_every_courtesy_kind_is_reported_together_in_order(
    baseline: _Baseline, at_046: str
) -> None:
    cluster = baseline.cluster
    for planter in _PLANTERS.values():
        await planter(cluster, at_046)
    before = await _catalog(cluster, at_046)
    code, output = await _alembic(cluster.url(at_046), "downgrade", "045")
    assert code != 0, output
    assert _refusal(output, _DOWNGRADE_REFUSED) == (
        f"{_DOWNGRADE_REFUSED}courtesy credit exists: courtesy lots, "
        "courtesy_removal ledger entries, credit_courtesy_events rows"
    )
    assert await _catalog(cluster, at_046) == before


@pytest.mark.parametrize(
    "statement,listed",
    [
        ("UPDATE alembic_version SET version_num = '045'", "045"),
        ("DELETE FROM alembic_version", "empty"),
        ("INSERT INTO alembic_version VALUES ('045')", "045, 046"),
    ],
)
async def test_the_downgrade_preflight_refuses_another_revision(
    baseline: _Baseline, at_046: str, statement: str, listed: str
) -> None:
    cluster = baseline.cluster
    await _execute(cluster, at_046, statement)
    before = await _catalog(cluster, at_046)
    engine = create_async_engine(cluster.url(at_046), poolclass=NullPool)
    try:
        with pytest.raises(DBAPIError, match="downgrade of migration 046") as info:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(_MIGRATION.LOCK_TIMEOUT)
                await connection.exec_driver_sql(_MIGRATION.downgrade_preflight())
    finally:
        await engine.dispose()
    assert _sqlstate(info.value) == "55000"
    assert f"{_DOWNGRADE_REFUSED}alembic_version is {listed}, not 046" in str(
        info.value
    )
    assert await _catalog(cluster, at_046) == before


@pytest.mark.parametrize(
    "table", ["credit_lots", "credit_ledger_entries", "credit_courtesy_events"]
)
async def test_the_downgrade_gives_up_after_the_lock_timeout(
    baseline: _Baseline, at_046: str, table: str
) -> None:
    """A reader of any table the preflight locks holds ACCESS SHARE: the
    preflight's ACCESS EXCLUSIVE lock gives up (55P03) before anything
    changes."""

    before = await _catalog(baseline.cluster, at_046)
    code, output = await _alembic_while_reading(
        baseline.cluster, at_046, table, "downgrade", "045"
    )
    _assert_lock_timeout_in_the_preflight(code, output)
    assert await _catalog(baseline.cluster, at_046) == before


@pytest.mark.parametrize(
    "table,altered",
    [
        ("credit_lots", "ALTER TABLE credit_lots"),
        ("credit_ledger_entries", "ALTER TABLE credit_ledger_entries"),
    ],
)
async def test_the_upgrade_gives_up_after_the_lock_timeout_and_is_one_transaction(
    baseline: _Baseline, at_045: str, table: str, altered: str
) -> None:
    """Each ALTER waits for a reader of its table, then gives up (55P03).
    Behind a reader of the ledger the lots' CHECKs were already swapped in
    the same transaction: they roll back with it, and the catalog is 045's."""

    code, output = await _alembic_while_reading(
        baseline.cluster, at_045, table, "upgrade", "046"
    )
    assert code is not None, "the migration waited past the bound: no lock timeout"
    assert code != 0, output
    assert "LockNotAvailableError" in output, output
    assert "canceling statement due to lock timeout" in output, output
    statements = re.findall(r"\[SQL:\s*(\S+ \S+)", output)
    assert statements == ["ALTER TABLE"], statements
    assert re.search(r"\[SQL:\s*" + re.escape(altered) + r"\b", output), output
    assert await _catalog(baseline.cluster, at_045) == baseline.catalog_045


# ---------------------------------------------------------------------------
# Window 046's reads.
# ---------------------------------------------------------------------------

_PREFLIGHT_SQL = (_ROOT / "scripts" / "migration_046_preflight.sql").read_text(
    encoding="utf-8"
)
_POST_READ_SQL = (_ROOT / "scripts" / "migration_046_post_read.sql").read_text(
    encoding="utf-8"
)


@pytest.mark.parametrize(
    "upgrade,statements,counts",
    [
        (None, (), (0, 0, 0)),
        # The 045 CHECK re-created from its source: still 045's.
        (
            None,
            (
                "ALTER TABLE credit_lots DROP CONSTRAINT ck_credit_lots_kind, "
                f"ADD CONSTRAINT ck_credit_lots_kind CHECK ({_MIGRATION.LOTS_KIND_045})",
            ),
            (0, 0, 0),
        ),
        # Drifted: already widened by hand, the revision still 045.
        (
            None,
            (
                "ALTER TABLE credit_lots DROP CONSTRAINT ck_credit_lots_kind, "
                f"ADD CONSTRAINT ck_credit_lots_kind CHECK ({_MIGRATION.LOTS_KIND_046})",
            ),
            (0, 1, 0),
        ),
        (
            None,
            (
                "ALTER TABLE credit_ledger_entries DROP CONSTRAINT "
                "ck_credit_ledger_entries_type",
            ),
            (0, 1, 0),
        ),
        (
            None,
            (
                "ALTER TABLE credit_lots DROP CONSTRAINT ck_credit_lots_source, "
                "ADD CONSTRAINT ck_credit_lots_source CHECK "
                f"({_MIGRATION.LOTS_SOURCE_045}) NOT VALID",
            ),
            (0, 1, 0),
        ),
        (
            None,
            ("ALTER TABLE credit_lots ALTER COLUMN payment_id DROP NOT NULL",),
            (0, 0, 1),
        ),
        ("046", (), (1, 3, 1)),
    ],
    ids=[
        "at-045",
        "045-recreated",
        "drifted",
        "missing",
        "not-valid",
        "payment-nullable",
        "at-046",
    ],
)
async def test_the_window_preflight_reads_zero_only_at_the_045_catalog(
    baseline: _Baseline,
    at_045: str,
    upgrade: str | None,
    statements: tuple[str, ...],
    counts: tuple[int, int, int],
) -> None:
    cluster = baseline.cluster
    if upgrade is not None:
        await _migrate(cluster.url(at_045), "upgrade", upgrade)
    if statements:
        await _execute(cluster, at_045, *statements)
    engine = create_async_engine(cluster.url(at_045), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            rows = (await connection.exec_driver_sql(_PREFLIGHT_SQL)).all()
    finally:
        await engine.dispose()
    assert [row[0] for row in rows] == [
        "046 tables present",
        "credit checks not at 045",
        "credit lot payment_id nullable",
    ]
    assert tuple(row[1] for row in rows) == counts


async def test_the_window_post_read_is_null_at_045_and_held_at_046(
    baseline: _Baseline, at_045: str
) -> None:
    cluster = baseline.cluster
    engine = create_async_engine(cluster.url(at_045), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            before = (await connection.exec_driver_sql(_POST_READ_SQL)).all()
    finally:
        await engine.dispose()
    await _migrate(cluster.url(at_045), "upgrade", "046")
    engine = create_async_engine(cluster.url(at_045), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            after = (await connection.exec_driver_sql(_POST_READ_SQL)).all()
    finally:
        await engine.dispose()
    name = "select and insert on public.credit_courtesy_events"
    assert [tuple(row) for row in before] == [(name, None)]
    assert [tuple(row) for row in after] == [(name, True)]
