"""Characterization of the canary triggers as migration 036 left them.

Migration 036 (Group 10 E5) rewrote the four functions that referenced the
legacy schema (``billing_migrations`` / the roster):
``protect_billing_canary_subscription``,
``validate_billing_canary_projection``, ``validate_billing_canary_checkout``
and ``validate_billing_canary_effect`` -- plus the two checkout CHECKs
``ck_billing_checkout_sessions_canary_exact`` and
``ck_billing_checkout_sessions_intent_target`` -- dropped the roster
statement trigger and the ``roster_revision``/``migration_id`` columns; 037
drops the legacy roster objects.  E1 wrote this module against 035 so the
rewrite would be a visible, deliberate change; E5 moved the probes that named
the removed objects to the 036 behaviour (section (e)).  The 035 definitions
themselves are frozen in ``alembic/frozen/036_downgrade_restores_035.json``
and proven by ``test_migration_036_postgres.py``.  These tests pin, on real
PostgreSQL at the head revision:

* every intentional trigger rejection pins the exact message, SQLSTATE
  ``23000`` and the PL/pgSQL function that raised it (never a bare
  ``raises``); every CHECK probe pins SQLSTATE ``23514`` and the constraint
  name, with all triggers off so the CHECK alone decides;
* every accepted write runs with ``SET CONSTRAINTS ALL IMMEDIATE`` and is
  rolled back, so the shared canary chain built once per module is never
  mutated by a test;
* the final test reads this module's PostgreSQL server log and requires its
  ERRORs to be exactly the registered intentional rejections (same messages,
  same counts).  It needs the smoke server started with
  ``logging_collector=on`` and a ``log_line_prefix`` containing ``%d`` (for
  example ``-c logging_collector=on -c "log_line_prefix=%m [%p] %d "``) and
  is skipped, with that reason, when the *server* is not configured so.

The smoke role must be a SUPERUSER (as the role of a disposable initdb
cluster is): the planting/CHECK probes set ``session_replication_role`` and
the log audit calls ``pg_current_logfile()``.  With a plain role (for example
``LOGIN CREATEDB``) the migrations still apply and the other tests pass, but
those 32 tests FAIL -- deliberately, not skip -- with one explicit message
naming the role (``_require_superuser``), like the sibling suites' CREATEDB
requirement, so the release gate cannot go green by skipping them.

What the rewritten ``protect_billing_canary_subscription`` does with a
non-ASAAS (``abacatepay`` or NULL provider) row is pinned in section (e2):
the row stays representable, its snapshot is immutable and a self-asserted
legacy cancellation is refused.

The canary chain is produced by the production services (checkout service,
webhook handoff, payment settlement), exactly like
``test_asaas_webhook_postgres.py``; only the negative probes are raw SQL.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from typing import Any, AsyncIterator, Sequence
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.repositories.billing import BillingRepository
from src.services.asaas import ProviderRecord, SubscriptionReadback
from src.services.billing_webhook import BillingReadbackService
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _run_alembic,
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_asaas_checkout_service_postgres import (
    _builder,
    _configure,
    _Flag,
    _Harness,
    _Provider,
    _service,
    _transition,
)
from tests.integration.test_asaas_webhook_postgres import (
    _deliver,
    _external_checkout,
    _payload,
    _payment,
    _Reads,
    _subscription_created,
    _wait_until_blocked,
)

pytestmark = pytest.mark.integration

_PROTECT = "protect_billing_canary_subscription"
_PROJECTION = "validate_billing_canary_projection"
_CHECKOUT = "validate_billing_canary_checkout"
_SLOT = "validate_billing_canary_checkout_slot"
_ROSTER_HISTORY = "protect_billing_roster_control"
_INTEGRITY = "23000"
_WAIT_SECONDS = 60.0

# Every rejection a test in this module provokes on purpose, counted.  The
# log-hygiene test (last in the module) requires the server log to hold
# exactly these ERRORs for this database: no extra one, no missing one.
_EXPECTED_ERRORS: Counter[str] = Counter()


@dataclass(frozen=True)
class _TriggerHarness(_Harness):
    database: str
    admin_url: URL


@dataclass(frozen=True)
class _Chain:
    """One ASAAS subscription produced by the production service path."""

    environment: str
    builder_id: UUID
    checkout_id: UUID
    external_checkout_id: str
    subscription_id: UUID
    external_subscription_id: str
    payment_id: str | None
    projection_id: UUID | None
    roster_revision_before: int
    roster_revision_after: int


@pytest.fixture(scope="module")
async def trigger_harness() -> AsyncIterator[_TriggerHarness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    engine = None
    created = False
    try:
        await _create_child(admin_engine, name)
        created = True
        url = admin_url.set(database=name)
        await _run_alembic(url, "upgrade", "head")
        engine = create_async_engine(url, poolclass=NullPool)
        yield _TriggerHarness(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
            database=name,
            admin_url=admin_url,
        )
    finally:
        if engine is not None:
            await engine.dispose()
        if created:
            await _drop_child(admin_engine, name)
        await admin_engine.dispose()


# ---------------------------------------------------------------------------
# Small SQL helpers.
# ---------------------------------------------------------------------------


async def _scalar(harness: _Harness, sql: str, **params: Any) -> Any:
    async with harness.session() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _row(harness: _Harness, sql: str, **params: Any) -> Any:
    async with harness.session() as session:
        return (await session.execute(text(sql), params)).one()


async def _roster_revision(harness: _Harness) -> int:
    return int(
        await _scalar(
            harness,
            "SELECT billing_roster_revision FROM billing_roster_control "
            "WHERE singleton = true",
        )
    )


def _postgres_cause(error: DBAPIError) -> Any:
    cause = getattr(error.orig, "__cause__", None)
    assert cause is not None and hasattr(cause, "sqlstate"), repr(error)
    return cause


def _assert_raised_by(
    error: DBAPIError, *, message: str, function: str, sqlstate: str = _INTEGRITY
) -> None:
    cause = _postgres_cause(error)
    assert _sqlstate(error) == sqlstate
    assert cause.message == message
    assert f"PL/pgSQL function {function}()" in (cause.context or ""), cause.context


Statement = tuple[str, dict[str, Any]]


async def _rejected(
    harness: _Harness,
    sql: str,
    params: dict[str, Any],
    *,
    message: str,
    function: str,
    setup: Sequence[Statement] = (),
    sqlstate: str = _INTEGRITY,
) -> None:
    """Run ``setup`` then ``sql`` in one transaction that must be refused."""

    _EXPECTED_ERRORS[message] += 1
    async with harness.session() as session:
        try:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            for statement, values in setup:
                await session.execute(text(statement), values)
            with pytest.raises(DBAPIError, match=re.escape(message)) as info:
                await session.execute(text(sql), params)
        finally:
            await session.rollback()
    _assert_raised_by(info.value, message=message, function=function, sqlstate=sqlstate)


async def _accepted(
    harness: _Harness,
    statements: Sequence[Statement],
    *,
    probe: Statement | None = None,
) -> tuple[list[int], Any]:
    """Run ``statements`` with every deferred check immediate, then roll back.

    Returns each statement's rowcount and the optional ``probe`` row read
    inside the same (rolled-back) transaction.
    """

    async with harness.session() as session:
        try:
            await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            counts = []
            for statement, values in statements:
                result = await session.execute(text(statement), values)
                counts.append(int(result.rowcount))  # type: ignore[attr-defined]
            observed = None
            if probe is not None:
                observed = (await session.execute(text(probe[0]), probe[1])).one()
        finally:
            await session.rollback()
    return counts, observed


_COLUMNS: dict[str, list[str]] = {}


async def _clone_sql(harness: _Harness, table: str, overrides: dict[str, str]) -> str:
    """``INSERT ... SELECT`` copying one row of ``table`` with SQL overrides."""

    if table not in _COLUMNS:
        async with harness.session() as session:
            rows = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table "
                    "ORDER BY ordinal_position"
                ),
                {"table": table},
            )
            _COLUMNS[table] = [row[0] for row in rows]
    columns = _COLUMNS[table]
    assert set(overrides) <= set(columns), sorted(set(overrides) - set(columns))
    selected = ", ".join(overrides.get(column, column) for column in columns)
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"SELECT {selected} FROM {table} WHERE id = :source_id"
    )


# ---------------------------------------------------------------------------
# Chains built once per module through the production services.
# ---------------------------------------------------------------------------


async def _create_subscription_checkout(
    harness: _Harness, environment: str, builder_id: UUID
) -> tuple[UUID, str]:
    provider = _Provider(harness)
    async with harness.session() as session:
        view = await _service(session, provider.client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )
    assert view.state == "active"
    return view.checkout_session_id, await _external_checkout(
        harness, view.checkout_session_id
    )


@pytest.fixture(scope="module")
async def canary(trigger_harness: _TriggerHarness) -> _Chain:
    """The ``development`` canary: checkout -> handoff -> settled payment."""

    harness = trigger_harness
    environment = "development"
    with pytest.MonkeyPatch.context() as patch:
        _configure(patch, environment, _Flag())
        builder_id = await _builder(harness, "G10 canary builder")
        await _transition(harness, environment, "canary_allowlist", builder_id)
        checkout_id, external_checkout_id = await _create_subscription_checkout(
            harness, environment, builder_id
        )
        before = await _roster_revision(harness)
        external_subscription_id = "sub_g10_canary"
        created = await _deliver(
            harness,
            _subscription_created(checkout_id, external_subscription_id, 99_900),
            environment,
        )
        assert created.outcome == "processed"
        payment_id = "pay_g10_canary"
        received = await _deliver(
            harness,
            _payment(
                "PAYMENT_RECEIVED",
                payment_id,
                checkout=external_checkout_id,
                subscription=external_subscription_id,
                value="999.00",
            ),
            environment,
        )
        assert received.outcome == "processed"
    subscription_id, projection_id = tuple(
        await _row(
            harness,
            "SELECT s.id, p.id FROM billing_subscriptions s "
            "JOIN billing_payment_projections p ON p.subscription_id = s.id "
            "WHERE s.builder_id = :builder AND p.canary_slot = 'subscription'",
            builder=builder_id,
        )
    )
    return _Chain(
        environment=environment,
        builder_id=builder_id,
        checkout_id=checkout_id,
        external_checkout_id=external_checkout_id,
        subscription_id=subscription_id,
        external_subscription_id=external_subscription_id,
        payment_id=payment_id,
        projection_id=projection_id,
        roster_revision_before=before,
        roster_revision_after=await _roster_revision(harness),
    )


@pytest.fixture(scope="module")
async def general(trigger_harness: _TriggerHarness) -> _Chain:
    """A non-canary ASAAS subscription (``test`` rollout is ``general``)."""

    harness = trigger_harness
    environment = "test"
    with pytest.MonkeyPatch.context() as patch:
        _configure(patch, environment, _Flag())
        # disabled -> canary_allowlist -> general, uncached: the sibling
        # modules memoize this per harness id / environment, which a fresh
        # database in the same process must not inherit.
        placeholder = await _builder(harness, "G10 general placeholder")
        await _transition(harness, environment, "canary_allowlist", placeholder)
        await _transition(harness, environment, "general")
        builder_id = await _builder(harness, "G10 general builder")
        checkout_id, external_checkout_id = await _create_subscription_checkout(
            harness, environment, builder_id
        )
        before = await _roster_revision(harness)
        external_subscription_id = "sub_g10_general"
        created = await _deliver(
            harness,
            _subscription_created(checkout_id, external_subscription_id, 99_900),
            environment,
        )
        assert created.outcome == "processed"
    subscription_id = await _scalar(
        harness,
        "SELECT id FROM billing_subscriptions WHERE builder_id = :builder",
        builder=builder_id,
    )
    return _Chain(
        environment=environment,
        builder_id=builder_id,
        checkout_id=checkout_id,
        external_checkout_id=external_checkout_id,
        subscription_id=subscription_id,
        external_subscription_id=external_subscription_id,
        payment_id=None,
        projection_id=None,
        roster_revision_before=before,
        roster_revision_after=await _roster_revision(harness),
    )


@pytest.fixture(scope="module")
async def general_paid(trigger_harness: _TriggerHarness, general: _Chain) -> _Chain:
    """A second non-canary ASAAS subscription with a settled payment.

    Separate from ``general`` so the projection/effect it owns (and their
    composite foreign keys) never constrain the probes on ``general``.
    """

    harness = trigger_harness
    environment = general.environment
    with pytest.MonkeyPatch.context() as patch:
        _configure(patch, environment, _Flag())
        builder_id = await _builder(harness, "G10 general paid builder")
        checkout_id, external_checkout_id = await _create_subscription_checkout(
            harness, environment, builder_id
        )
        before = await _roster_revision(harness)
        external_subscription_id = "sub_g10_general_paid"
        created = await _deliver(
            harness,
            _subscription_created(checkout_id, external_subscription_id, 99_900),
            environment,
        )
        assert created.outcome == "processed"
        payment_id = "pay_g10_general_paid"
        received = await _deliver(
            harness,
            _payment(
                "PAYMENT_RECEIVED",
                payment_id,
                checkout=external_checkout_id,
                subscription=external_subscription_id,
                value="999.00",
            ),
            environment,
        )
        assert received.outcome == "processed"
    subscription_id, projection_id = tuple(
        await _row(
            harness,
            "SELECT s.id, p.id FROM billing_subscriptions s "
            "JOIN billing_payment_projections p ON p.subscription_id = s.id "
            "WHERE s.builder_id = :builder",
            builder=builder_id,
        )
    )
    return _Chain(
        environment=environment,
        builder_id=builder_id,
        checkout_id=checkout_id,
        external_checkout_id=external_checkout_id,
        subscription_id=subscription_id,
        external_subscription_id=external_subscription_id,
        payment_id=payment_id,
        projection_id=projection_id,
        roster_revision_before=before,
        roster_revision_after=await _roster_revision(harness),
    )


async def _effect_id(harness: _Harness, chain: _Chain) -> UUID:
    return UUID(
        str(
            await _scalar(
                harness,
                "SELECT id FROM billing_payment_effects "
                "WHERE projection_id = :projection AND effect_kind = 'settlement'",
                projection=chain.projection_id,
            )
        )
    )


async def test_service_chains_produce_the_rows_the_triggers_guard(
    trigger_harness: _TriggerHarness, canary: _Chain, general: _Chain
) -> None:
    """Baseline: the shapes every later probe starts from (not a trigger probe)."""

    harness = trigger_harness
    canary_row = await _row(
        harness,
        "SELECT provider, external_subscription_id, plan, billing_cycle, "
        "coupon_code, catalog_version_id, offer_id, amount_cents, status, "
        "is_active FROM billing_subscriptions WHERE id = :id",
        id=canary.subscription_id,
    )
    assert tuple(canary_row) == (
        "asaas",
        "sub_g10_canary",
        "plus",
        "monthly",
        None,
        "payment-2026-10-03",
        "plan_plus_monthly_v1",
        99_900,
        "active",
        True,
    )
    general_row = await _row(
        harness,
        "SELECT provider, status, is_active, "
        "(SELECT count(*) FROM billing_payment_projections p "
        " WHERE p.subscription_id = s.id) FROM billing_subscriptions s "
        "WHERE id = :id",
        id=general.subscription_id,
    )
    assert tuple(general_row) == ("asaas", "active_linked", True, 0)
    projection = await _row(
        harness,
        "SELECT builder_id, provider, aggregate_kind, subscription_id, "
        "checkout_session_id, canary_environment, canary_slot, status, "
        "amount_cents, settled_cents FROM billing_payment_projections "
        "WHERE id = :id",
        id=canary.projection_id,
    )
    assert tuple(projection) == (
        canary.builder_id,
        "asaas",
        "subscription",
        canary.subscription_id,
        canary.checkout_id,
        "development",
        "subscription",
        "settled",
        99_900,
        99_900,
    )
    checkout = await _row(
        harness,
        "SELECT state, intent_id, external_checkout_id, "
        "resulting_external_subscription_id, canary_environment, canary_slot "
        "FROM billing_checkout_sessions WHERE id = :id",
        id=canary.checkout_id,
    )
    assert tuple(checkout) == (
        "completed",
        canary.subscription_id,
        canary.external_checkout_id,
        "sub_g10_canary",
        "development",
        "subscription",
    )
    general_checkout = await _row(
        harness,
        "SELECT state, intent_id, external_checkout_id, "
        "resulting_external_subscription_id, canary_environment, canary_slot "
        "FROM billing_checkout_sessions WHERE id = :id",
        id=general.checkout_id,
    )
    assert tuple(general_checkout) == (
        "completed",
        general.subscription_id,
        general.external_checkout_id,
        "sub_g10_general",
        None,
        None,
    )
    effect = await _row(
        harness,
        "SELECT provider, payment_id, effect_kind, source_revision, amount_cents, "
        "canary_environment, canary_slot FROM billing_payment_effects "
        "WHERE projection_id = :projection",
        projection=canary.projection_id,
    )
    assert tuple(effect) == (
        "asaas",
        "pay_g10_canary",
        "settlement",
        "settlement:v1",
        99_900,
        "development",
        "subscription",
    )


# ---------------------------------------------------------------------------
# (a) ASAAS write-once snapshot on billing_subscriptions.
# ---------------------------------------------------------------------------

_SNAPSHOT_MUTATIONS = {
    # A provider no CHECK admits: the BEFORE trigger refuses it first, so
    # the pinned rejection does not depend on which legacy provider exists.
    "provider_unknown": "provider = 'g10_unknown'",
    "provider_null": "provider = NULL",
    "external_subscription_id": "external_subscription_id = 'sub_g10_rewritten'",
    "plan": "plan = 'pro'",
    "billing_cycle": "billing_cycle = 'annual'",
    "coupon_code": "coupon_code = 'G10COUPON'",
    "catalog_version_id": "catalog_version_id = 'catalog-v2'",
    "offer_id": "offer_id = 'plan_pro_monthly_v1'",
    "amount_cents": "amount_cents = amount_cents + 1",
}


@pytest.mark.parametrize("kind", ["general", "canary"])
@pytest.mark.parametrize("field", sorted(_SNAPSHOT_MUTATIONS))
async def test_asaas_subscription_snapshot_field_is_write_once(
    trigger_harness: _TriggerHarness,
    canary: _Chain,
    general: _Chain,
    kind: str,
    field: str,
) -> None:
    chain = canary if kind == "canary" else general
    await _rejected(
        trigger_harness,
        f"UPDATE billing_subscriptions SET {_SNAPSHOT_MUTATIONS[field]} "
        "WHERE id = :id",
        {"id": chain.subscription_id},
        message="provider subscription snapshot is write-once",
        function=_PROTECT,
    )


async def test_general_asaas_subscription_keeps_status_activity_dates_and_ids_mutable(
    trigger_harness: _TriggerHarness, general: _Chain
) -> None:
    """Without a canary projection nothing but the snapshot tuple is guarded.

    Status may move in any direction (including ``cancelled`` back to
    ``active``), timestamps are free, and even ``id`` is not
    trigger-protected for a non-canary ASAAS row.  (``builder_id`` is not
    probed: the composite foreign keys of the checkout/projection rows that
    reference it would refuse the change before the trigger matters.)
    """

    update = "UPDATE billing_subscriptions SET {} WHERE id = :id"
    by_id = {"id": general.subscription_id}
    counts, observed = await _accepted(
        trigger_harness,
        [
            (update.format("status = 'readback_required'"), by_id),
            (update.format("status = 'active'"), by_id),
            (update.format("status = 'active_linked'"), by_id),
            (update.format("status = 'inactive', is_active = false"), by_id),
            (update.format("status = 'cancelled'"), by_id),
            (update.format("status = 'active', is_active = true"), by_id),
            (
                update.format(
                    "created_at = created_at - interval '1 day', "
                    "updated_at = updated_at + interval '1 day'"
                ),
                by_id,
            ),
            (update.format("status = status"), by_id),
        ],
        probe=(
            "SELECT status, is_active FROM billing_subscriptions WHERE id = :id",
            by_id,
        ),
    )
    assert counts == [1] * 8
    assert tuple(observed) == ("active", True)

    new_id = uuid4()
    counts, observed = await _accepted(
        trigger_harness,
        [(update.format("id = :new_id"), {**by_id, "new_id": new_id})],
        probe=(
            "SELECT count(*) FROM billing_subscriptions WHERE id = :new_id",
            {"new_id": new_id},
        ),
    )
    assert counts == [1] and tuple(observed) == (1,)


async def test_general_asaas_subscription_delete_is_silently_skipped(
    trigger_harness: _TriggerHarness, general: _Chain
) -> None:
    """The BEFORE DELETE row trigger returns ``NEW`` (NULL on DELETE).

    PostgreSQL therefore skips the row: the DELETE reports 0 rows and the
    subscription survives, without any error.
    """

    counts, observed = await _accepted(
        trigger_harness,
        [
            (
                "DELETE FROM billing_subscriptions WHERE id = :id",
                {"id": general.subscription_id},
            )
        ],
        probe=(
            "SELECT count(*) FROM billing_subscriptions WHERE id = :id",
            {"id": general.subscription_id},
        ),
    )
    assert counts == [0]
    assert tuple(observed) == (1,)


# ---------------------------------------------------------------------------
# (b) Canary immutability and lifecycle (026 L2162-2207).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE billing_subscriptions SET id = gen_random_uuid() WHERE id = :id",
        "UPDATE billing_subscriptions SET builder_id = :other WHERE id = :id",
        "DELETE FROM billing_subscriptions WHERE id = :id",
    ],
    ids=["id", "builder_id", "delete"],
)
async def test_canary_subscription_identity_and_delete_are_immutable(
    trigger_harness: _TriggerHarness,
    canary: _Chain,
    general: _Chain,
    statement: str,
) -> None:
    params: dict[str, Any] = {"id": canary.subscription_id}
    if ":other" in statement:
        params["other"] = general.builder_id
    await _rejected(
        trigger_harness,
        statement,
        params,
        message="canary subscription snapshot is immutable",
        function=_PROTECT,
    )


_LIVE = ("active", "active_linked", "readback_required")
_LIFECYCLE_SOURCES = (
    "active",
    "active_linked",
    "readback_required",
    "non_billable_pending_readback",
    "inactive",
    "cancelled",
)
_ALL_STATUSES = _LIFECYCLE_SOURCES + (
    "legacy_cancel_pending",
    "legacy_cancelled",
    "provider_unknown",
    "manual_intervention",
)
_FROM_LIVE = {
    "active_linked",
    "inactive",
    "cancelled",
    "readback_required",
    "non_billable_pending_readback",
}
_ALLOWED_TRANSITIONS = {
    "active": _FROM_LIVE,
    "active_linked": _FROM_LIVE,
    "readback_required": {
        "active",
        "active_linked",
        "inactive",
        "cancelled",
        "non_billable_pending_readback",
    },
    "non_billable_pending_readback": {"inactive", "cancelled"},
    "inactive": {"cancelled"},
    "cancelled": set(),
}


def _lifecycle_expectation(source: str, target: str) -> str | None:
    if source not in _LIVE and target in _LIVE:
        return "canary subscription cannot reactivate"
    if target not in _ALLOWED_TRANSITIONS[source]:
        return "canary subscription lifecycle regressed"
    return None


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (source, target)
        for source in _LIFECYCLE_SOURCES
        for target in _ALL_STATUSES
        if source != target
    ],
)
async def test_canary_subscription_lifecycle_matrix(
    trigger_harness: _TriggerHarness, canary: _Chain, source: str, target: str
) -> None:
    update = (
        "UPDATE billing_subscriptions SET status = :status, is_active = :active "
        "WHERE id = :id"
    )
    setup: list[Statement] = []
    if source != "active":
        setup.append(
            (
                update,
                {
                    "id": canary.subscription_id,
                    "status": source,
                    "active": source in _LIVE,
                },
            )
        )
    params = {
        "id": canary.subscription_id,
        "status": target,
        "active": target in _LIVE,
    }
    expected = _lifecycle_expectation(source, target)
    if expected is None:
        counts, observed = await _accepted(
            trigger_harness,
            [*setup, (update, params)],
            probe=(
                "SELECT status, is_active FROM billing_subscriptions WHERE id = :id",
                {"id": canary.subscription_id},
            ),
        )
        assert counts[-1] == 1
        assert tuple(observed) == (target, target in _LIVE)
    else:
        await _rejected(
            trigger_harness,
            update,
            params,
            setup=setup,
            message=expected,
            function=_PROTECT,
        )


async def test_canary_subscription_status_rewrite_and_timestamps_are_accepted(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    by_id = {"id": canary.subscription_id}
    counts, observed = await _accepted(
        trigger_harness,
        [
            (
                "UPDATE billing_subscriptions SET status = status, "
                "updated_at = updated_at + interval '1 day', "
                "created_at = created_at - interval '1 day' WHERE id = :id",
                by_id,
            )
        ],
        probe=("SELECT status FROM billing_subscriptions WHERE id = :id", by_id),
    )
    assert counts == [1] and tuple(observed) == ("active",)


async def test_canary_subscription_reactivation_is_refused_even_with_legal_status(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    """``inactive -> cancelled`` is legal, but not while flipping ``is_active``."""

    await _rejected(
        trigger_harness,
        "UPDATE billing_subscriptions SET status = 'cancelled', is_active = true "
        "WHERE id = :id",
        {"id": canary.subscription_id},
        setup=[
            (
                "UPDATE billing_subscriptions SET status = 'inactive', "
                "is_active = false WHERE id = :id",
                {"id": canary.subscription_id},
            )
        ],
        message="canary subscription cannot reactivate",
        function=_PROTECT,
    )


@pytest.mark.parametrize("target", _LIVE)
@pytest.mark.parametrize(
    "source", ["non_billable_pending_readback", "inactive", "cancelled"]
)
async def test_canary_subscription_lifecycle_guard_holds_without_is_active_flip(
    trigger_harness: _TriggerHarness, canary: _Chain, source: str, target: str
) -> None:
    """A non-live -> live status move that keeps ``is_active = false``.

    The reactivation guard (``is_active`` false -> true) does not fire, so the
    lifecycle allowlist alone refuses it -- before the
    ``ck_billing_subscriptions_active_status`` CHECK is ever evaluated.
    """

    update = (
        "UPDATE billing_subscriptions SET status = :status, is_active = false "
        "WHERE id = :id"
    )
    await _rejected(
        trigger_harness,
        update,
        {"id": canary.subscription_id, "status": target},
        # active -> source is itself an allowed move.
        setup=[(update, {"id": canary.subscription_id, "status": source})],
        message="canary subscription lifecycle regressed",
        function=_PROTECT,
    )


# ---------------------------------------------------------------------------
# (c) validate_billing_canary_projection lineage.
# ---------------------------------------------------------------------------


async def test_canary_projection_accepts_mutable_writes_on_its_lineage(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    by_id = {"id": canary.projection_id}
    counts, observed = await _accepted(
        trigger_harness,
        [
            (
                "UPDATE billing_payment_projections SET "
                "updated_at = updated_at + interval '1 day', "
                "last_event_id = 'evt_g10_touch' WHERE id = :id",
                by_id,
            )
        ],
        probe=(
            "SELECT last_event_id FROM billing_payment_projections WHERE id = :id",
            by_id,
        ),
    )
    assert counts == [1] and tuple(observed) == ("evt_g10_touch",)


_PROJECTION_IDENTITY = {
    "id": "id = gen_random_uuid()",
    "builder_id": "builder_id = :other_builder",
    "provider": "provider = 'g10_unknown'",
    "payment_id": "payment_id = 'pay_g10_rewritten'",
    "aggregate_kind": "aggregate_kind = 'topup'",
    "subscription_id": "subscription_id = :other_subscription",
    "topup_order_id": "topup_order_id = gen_random_uuid()",
    "checkout_session_id": "checkout_session_id = :other_checkout",
    "amount_cents": "amount_cents = amount_cents + 1",
    "canary_environment": "canary_environment = 'staging'",
    "canary_slot": "canary_slot = 'topup'",
}


@pytest.mark.parametrize("field", sorted(_PROJECTION_IDENTITY))
async def test_canary_projection_identity_is_immutable(
    trigger_harness: _TriggerHarness, canary: _Chain, general: _Chain, field: str
) -> None:
    await _rejected(
        trigger_harness,
        f"UPDATE billing_payment_projections SET {_PROJECTION_IDENTITY[field]} "
        "WHERE id = :id",
        {
            "id": canary.projection_id,
            "other_builder": general.builder_id,
            "other_subscription": general.subscription_id,
            "other_checkout": general.checkout_id,
        },
        message="canary projection identity is immutable",
        function=_PROJECTION,
    )


def _projection_cases(canary: _Chain, general: _Chain) -> dict[str, Any]:
    """INSERT copies of the canary projection that break one lineage rule."""

    fresh = {"id": "gen_random_uuid()", "payment_id": "'pay_g10_' || md5(id::text)"}
    return {
        # Canary subscription, but the copy drops its checkout/provenance.
        "historical_lineage_dropped": (
            {
                **fresh,
                "checkout_session_id": "NULL",
                "canary_environment": "NULL",
                "canary_slot": "NULL",
            },
            "historical canary lineage is required",
        ),
        # Canary checkout, but the subscription of ANOTHER builder.
        "subscription_of_another_builder": (
            {**fresh, "subscription_id": f"'{general.subscription_id}'::uuid"},
            "canary subscription projection target mismatch",
        ),
        # Canary checkout and another builder's subscription without provenance.
        "checkout_without_provenance": (
            {
                **fresh,
                "subscription_id": f"'{general.subscription_id}'::uuid",
                "canary_environment": "NULL",
                "canary_slot": "NULL",
            },
            "canary projection provenance is required",
        ),
        # ANOTHER builder claims the canary subscription/checkout lineage.
        "lineage_claimed_by_another_builder": (
            {**fresh, "builder_id": f"'{general.builder_id}'::uuid"},
            "canary projection requires canary checkout",
        ),
        # The other builder's own (non-canary) lineage stamped as canary.
        "non_canary_checkout_stamped_canary": (
            {
                **fresh,
                "builder_id": f"'{general.builder_id}'::uuid",
                "subscription_id": f"'{general.subscription_id}'::uuid",
                "checkout_session_id": f"'{general.checkout_id}'::uuid",
            },
            "canary projection requires canary checkout",
        ),
        # A canary builder's ASAAS projection without any canary provenance.
        "canary_builder_without_provenance": (
            {
                **fresh,
                "subscription_id": f"'{general.subscription_id}'::uuid",
                "checkout_session_id": "NULL",
                "canary_environment": "NULL",
                "canary_slot": "NULL",
            },
            "active canary projection provenance is required",
        ),
    }


_PROJECTION_CASE_IDS = [
    "historical_lineage_dropped",
    "subscription_of_another_builder",
    "checkout_without_provenance",
    "lineage_claimed_by_another_builder",
    "non_canary_checkout_stamped_canary",
    "canary_builder_without_provenance",
]


@pytest.mark.parametrize("case", _PROJECTION_CASE_IDS)
async def test_canary_projection_insert_requires_exact_lineage(
    trigger_harness: _TriggerHarness, canary: _Chain, general: _Chain, case: str
) -> None:
    overrides, message = _projection_cases(canary, general)[case]
    await _rejected(
        trigger_harness,
        await _clone_sql(trigger_harness, "billing_payment_projections", overrides),
        {"source_id": canary.projection_id},
        message=message,
        function=_PROJECTION,
    )


@pytest.mark.parametrize(
    "subscription_state",
    [
        "status = 'inactive', is_active = false",
        # Still live and is_active, but not status 'active'.
        "status = 'active_linked'",
    ],
    ids=["inactive", "active_linked"],
)
async def test_canary_projection_write_requires_canonical_active_plus(
    trigger_harness: _TriggerHarness, canary: _Chain, subscription_state: str
) -> None:
    await _rejected(
        trigger_harness,
        "UPDATE billing_payment_projections SET updated_at = now() WHERE id = :id",
        {"id": canary.projection_id},
        setup=[
            (
                f"UPDATE billing_subscriptions SET {subscription_state} "
                "WHERE id = :id",
                {"id": canary.subscription_id},
            )
        ],
        message="canonical Plus subscription is required",
        function=_PROJECTION,
    )


@pytest.mark.parametrize(
    ("planted", "message"),
    [
        (
            "resulting_external_subscription_id = 'sub_g10_planted'",
            "provider subscription lineage is required",
        ),
        ("external_checkout_id = NULL", "provider checkout identity is required"),
    ],
    ids=["subscription_lineage", "checkout_identity"],
)
async def test_canary_projection_rejects_planted_provider_identity_drift(
    trigger_harness: _TriggerHarness, canary: _Chain, planted: str, message: str
) -> None:
    """States the write-once checkout trigger forbids, planted with triggers off.

    ``session_replication_role = replica`` (superuser, rolled back) is the only
    way to reach them; the projection trigger must still refuse the lineage.
    """

    await _require_superuser(trigger_harness)
    await _rejected(
        trigger_harness,
        "UPDATE billing_payment_projections SET updated_at = now() WHERE id = :id",
        {"id": canary.projection_id},
        setup=[
            ("SET LOCAL session_replication_role = replica", {}),
            (
                f"UPDATE billing_checkout_sessions SET {planted} WHERE id = :id",
                {"id": canary.checkout_id},
            ),
            ("SET LOCAL session_replication_role = origin", {}),
        ],
        message=message,
        function=_PROJECTION,
    )


_SUPERUSER_REQUIRED = (
    "this module needs a SUPERUSER smoke role: the planting and CHECK probes "
    "set session_replication_role and the log audit calls "
    "pg_current_logfile(); connected role {role!r} is not a superuser. "
    "Point POSTGRES_SMOKE_DATABASE_URL at a disposable cluster's superuser "
    "(the release gate must not pass by skipping these probes)."
)


async def _superuser_problem(session: AsyncSession) -> str | None:
    """The failure message when ``current_user`` is not a superuser."""

    role, is_super = (
        await session.execute(
            text(
                "SELECT current_user, rolsuper FROM pg_roles "
                "WHERE rolname = current_user"
            )
        )
    ).one()
    return None if is_super else _SUPERUSER_REQUIRED.format(role=role)


async def _require_superuser(harness: _Harness) -> None:
    """FAIL (never skip) when the smoke role cannot run superuser probes.

    Same policy as the sibling suites' CREATEDB requirement: a missing
    capability of the smoke role is an error, so a release gate run on an
    under-privileged role cannot go green by skipping.
    """

    async with harness.session() as session:
        problem = await _superuser_problem(session)
    if problem is not None:
        pytest.fail(problem, pytrace=False)


async def test_superuser_requirement_fails_clearly_for_a_plain_role(
    trigger_harness: _TriggerHarness,
) -> None:
    """Proof of the policy above, without a second login role.

    Inside one rolled-back transaction, ``SET ROLE`` to a fresh NOLOGIN,
    non-superuser role: the requirement reports it by name, and
    ``pg_current_logfile()`` -- what the log audit needs -- is refused with
    exactly the error a ``LOGIN CREATEDB`` smoke role would hit.
    """

    harness = trigger_harness
    await _require_superuser(harness)
    role = f"g10_plain_{uuid4().hex[:12]}"
    denied = "permission denied for function pg_current_logfile"
    _EXPECTED_ERRORS[denied] += 1
    async with harness.session() as session:
        try:
            await session.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
            await session.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert await _superuser_problem(session) == _SUPERUSER_REQUIRED.format(
                role=role
            )
            with pytest.raises(DBAPIError, match=re.escape(denied)) as info:
                await session.execute(text("SELECT pg_current_logfile()"))
        finally:
            await session.rollback()
    assert _sqlstate(info.value) == "42501"
    assert _postgres_cause(info.value).message == denied


_PLANT = "SET LOCAL session_replication_role = replica"
_UNPLANT = "SET LOCAL session_replication_role = origin"
_TOPUP_SNAPSHOT = (
    '{"catalog_version_id": "payment-2026-10-03", '
    '"offer_id": "package_credits_500_v1", "amount_cents": 10000, '
    '"credit_grant_mc": 500000}'
)


async def _planted_topup_checkout(harness: _Harness, canary: _Chain) -> Statement:
    """A completed ``development``/``topup`` canary checkout, triggers off.

    No production path creates a topup canary here; the row satisfies every
    CHECK (``ck_billing_checkout_sessions_canary_exact`` topup branch) and is
    only ever planted inside a rolled-back transaction.
    """

    insert = await _clone_sql(
        harness,
        "billing_checkout_sessions",
        {
            "id": ":checkout",
            "intent_kind": "'topup'",
            "intent_id": ":order",
            "topup_order_id": ":order",
            "offer_id": "'package_credits_500_v1'",
            "amount_cents": "10000",
            "pricing_snapshot": "CAST(:snapshot AS jsonb)",
            "external_checkout_id": "'chk_g10_topup'",
            "resulting_external_subscription_id": "NULL",
            "canary_slot": "'topup'",
        },
    )
    return insert, {"source_id": canary.checkout_id, "snapshot": _TOPUP_SNAPSHOT}


async def test_canary_topup_projection_requires_the_checkout_topup_target(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    harness = trigger_harness
    await _require_superuser(harness)
    checkout_id, order_id = uuid4(), uuid4()
    plant, plant_params = await _planted_topup_checkout(harness, canary)
    insert = await _clone_sql(
        harness,
        "billing_payment_projections",
        {
            "id": "gen_random_uuid()",
            "payment_id": "'pay_g10_topup'",
            "aggregate_kind": "'topup'",
            "subscription_id": "NULL",
            # Not the checkout's topup order.
            "topup_order_id": "gen_random_uuid()",
            "checkout_session_id": ":checkout",
            "canary_slot": "'topup'",
            "amount_cents": "10000",
            "settled_cents": "0",
            "status": "'pending'",
        },
    )
    await _rejected(
        harness,
        insert,
        {"source_id": canary.projection_id, "checkout": checkout_id},
        setup=[
            (_PLANT, {}),
            (plant, {**plant_params, "checkout": checkout_id, "order": order_id}),
            (_UNPLANT, {}),
        ],
        message="canary topup projection target mismatch",
        function=_PROJECTION,
    )


async def test_general_payment_projection_and_effect_are_accepted(
    trigger_harness: _TriggerHarness, general_paid: _Chain
) -> None:
    """The production settlement of a non-canary subscription passes all
    three triggers, and so do raw copies of its projection and effect."""

    harness = trigger_harness
    observed = await _row(
        harness,
        "SELECT p.provider, p.aggregate_kind, p.subscription_id, "
        "p.checkout_session_id, p.canary_environment, p.canary_slot, p.status, "
        "p.settled_cents, e.effect_kind, e.source_revision, e.amount_cents, "
        "e.canary_environment, e.canary_slot "
        "FROM billing_payment_projections p "
        "JOIN billing_payment_effects e ON e.projection_id = p.id "
        "WHERE p.id = :id",
        id=general_paid.projection_id,
    )
    assert tuple(observed) == (
        "asaas",
        "subscription",
        general_paid.subscription_id,
        general_paid.checkout_id,
        None,
        None,
        "settled",
        99_900,
        "settlement",
        "settlement:v1",
        99_900,
        None,
        None,
    )
    effect_id = await _effect_id(harness, general_paid)
    projection_copy = uuid4()
    counts, probe = await _accepted(
        harness,
        [
            (
                "UPDATE billing_payment_projections SET "
                "updated_at = updated_at + interval '1 day', "
                "last_event_id = 'evt_g10_general' WHERE id = :id",
                {"id": general_paid.projection_id},
            ),
            (
                await _clone_sql(
                    harness,
                    "billing_payment_projections",
                    {"id": ":copy", "payment_id": "'pay_g10_general_copy'"},
                ),
                {"source_id": general_paid.projection_id, "copy": projection_copy},
            ),
            (
                await _clone_sql(
                    harness,
                    "billing_payment_effects",
                    {
                        "id": "gen_random_uuid()",
                        "effect_kind": "'reversal'",
                        "source_revision": "'reversal:g10'",
                    },
                ),
                {"source_id": effect_id},
            ),
            (
                await _clone_sql(
                    harness,
                    "billing_payment_effects",
                    {"id": "gen_random_uuid()", "source_revision": "'settlement:g10'"},
                ),
                {"source_id": effect_id},
            ),
            (
                "UPDATE billing_payment_effects SET created_at = created_at "
                "WHERE id = :id",
                {"id": effect_id},
            ),
        ],
        probe=(
            "SELECT (SELECT count(*) FROM billing_payment_projections "
            "        WHERE subscription_id = :sub), "
            "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b)",
            {"sub": general_paid.subscription_id, "b": general_paid.builder_id},
        ),
    )
    assert counts == [1] * 5
    assert tuple(probe) == (2, 3)


async def test_general_projection_rejections(
    trigger_harness: _TriggerHarness, canary: _Chain, general_paid: _Chain
) -> None:
    """The refusals that apply to a non-canary projection, INSERT and UPDATE."""

    harness = trigger_harness
    by_id = {"id": general_paid.projection_id}
    # UPDATE that stamps canary tags on a non-canary projection.
    await _rejected(
        harness,
        "UPDATE billing_payment_projections SET canary_environment = 'test', "
        "canary_slot = 'subscription' WHERE id = :id",
        by_id,
        message="canary projection identity is immutable",
        function=_PROJECTION,
    )
    # INSERT of a copy stamped canary while its checkout is not a canary one.
    await _rejected(
        harness,
        await _clone_sql(
            harness,
            "billing_payment_projections",
            {
                "id": "gen_random_uuid()",
                "payment_id": "'pay_g10_general_stamped'",
                "canary_environment": "'test'",
                "canary_slot": "'subscription'",
            },
        ),
        {"source_id": general_paid.projection_id},
        message="canary projection requires canary checkout",
        function=_PROJECTION,
    )
    # UPDATE (not INSERT) re-pointing a non-canary projection at the canary
    # subscription: the historical canary lineage is checked on UPDATE too.
    await _rejected(
        harness,
        "UPDATE billing_payment_projections SET builder_id = :builder, "
        "subscription_id = :subscription WHERE id = :id",
        {
            **by_id,
            "builder": canary.builder_id,
            "subscription": canary.subscription_id,
        },
        message="historical canary lineage is required",
        function=_PROJECTION,
    )


# ---------------------------------------------------------------------------
# (c2) validate_billing_canary_effect (026 L2218; since 036 the historical
# settlement lineage is the canary checkout's intent alone, no billing_migrations).
# ---------------------------------------------------------------------------

_EFFECT = "validate_billing_canary_effect"


async def test_canary_effect_is_immutable(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    await _rejected(
        trigger_harness,
        "UPDATE billing_payment_effects SET created_at = created_at WHERE id = :id",
        {"id": await _effect_id(trigger_harness, canary)},
        message="canary settlement effect is immutable",
        function=_EFFECT,
    )


_EFFECT_COPIES: dict[str, tuple[dict[str, str], str]] = {
    # Settlement of the canary projection without its canary tags.
    "settlement_without_tags": (
        {
            "source_revision": "'settlement:g10'",
            "canary_environment": "NULL",
            "canary_slot": "NULL",
        },
        "canary settlement provenance is required",
    ),
    "settlement_with_other_slot": (
        {"source_revision": "'settlement:g10'", "canary_slot": "'topup'"},
        "canary settlement provenance is required",
    ),
    "settlement_source_revision": (
        {"source_revision": "'settlement:v2'"},
        "canary settlement is not fully settled",
    ),
    # Same idempotency key as the real settlement: the trigger refuses it
    # before the unique index (or the effect CHECK) is ever consulted.
    "settlement_amount": (
        {"amount_cents": "99899"},
        "canary settlement is not fully settled",
    ),
    # Canary tags on a non-settlement effect.
    "reversal_with_tags": (
        {"effect_kind": "'reversal'", "source_revision": "'reversal:g10'"},
        "canary tags are settlement-only",
    ),
}


@pytest.mark.parametrize("case", sorted(_EFFECT_COPIES))
async def test_canary_effect_insert_rules(
    trigger_harness: _TriggerHarness, canary: _Chain, case: str
) -> None:
    overrides, message = _EFFECT_COPIES[case]
    await _rejected(
        trigger_harness,
        await _clone_sql(
            trigger_harness,
            "billing_payment_effects",
            {"id": "gen_random_uuid()", **overrides},
        ),
        {"source_id": await _effect_id(trigger_harness, canary)},
        message=message,
        function=_EFFECT,
    )


@pytest.mark.parametrize(
    "projection_state",
    [
        "status = 'pending', settled_cents = 0",
        # Fully settled amounts, but not status 'settled'.
        "status = 'readback_required'",
    ],
    ids=["pending", "readback_required"],
)
async def test_canary_effect_requires_a_fully_settled_projection(
    trigger_harness: _TriggerHarness, canary: _Chain, projection_state: str
) -> None:
    """A correctly tagged settlement copy while the projection is not settled."""

    await _rejected(
        trigger_harness,
        await _clone_sql(
            trigger_harness,
            "billing_payment_effects",
            {"id": "gen_random_uuid()"},
        ),
        {"source_id": await _effect_id(trigger_harness, canary)},
        setup=[
            (
                f"UPDATE billing_payment_projections SET {projection_state} "
                "WHERE id = :id",
                {"id": canary.projection_id},
            )
        ],
        message="canary settlement is not fully settled",
        function=_EFFECT,
    )


async def test_general_effect_with_canary_tags_is_refused(
    trigger_harness: _TriggerHarness, general_paid: _Chain
) -> None:
    """Even a settlement: the projection is not canary, so tags are refused."""

    await _rejected(
        trigger_harness,
        await _clone_sql(
            trigger_harness,
            "billing_payment_effects",
            {
                "id": "gen_random_uuid()",
                "source_revision": "'settlement:g10'",
                "canary_environment": "'test'",
                "canary_slot": "'subscription'",
            },
        ),
        {"source_id": await _effect_id(trigger_harness, general_paid)},
        message="canary tags are settlement-only",
        function=_EFFECT,
    )


async def test_canary_effect_requires_historical_settlement_lineage(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    """A settlement on an untagged projection of the canary subscription.

    The projection trigger forbids such a projection, so it is planted with
    triggers off (superuser, rolled back); the effect trigger must still
    refuse the settlement because a canary checkout owns that subscription.
    """

    harness = trigger_harness
    await _require_superuser(harness)
    projection = uuid4()
    payment = "pay_g10_untagged"
    await _rejected(
        harness,
        await _clone_sql(
            harness,
            "billing_payment_effects",
            {
                "id": "gen_random_uuid()",
                "projection_id": ":projection",
                "payment_id": ":payment",
                "canary_environment": "NULL",
                "canary_slot": "NULL",
            },
        ),
        {
            "source_id": await _effect_id(harness, canary),
            "projection": projection,
            "payment": payment,
        },
        setup=[
            (_PLANT, {}),
            (
                await _clone_sql(
                    harness,
                    "billing_payment_projections",
                    {
                        "id": ":projection",
                        "payment_id": ":payment",
                        "checkout_session_id": "NULL",
                        "canary_environment": "NULL",
                        "canary_slot": "NULL",
                    },
                ),
                {
                    "source_id": canary.projection_id,
                    "projection": projection,
                    "payment": payment,
                },
            ),
            (_UNPLANT, {}),
        ],
        message="historical canary settlement lineage is required",
        function=_EFFECT,
    )


# ---------------------------------------------------------------------------
# (d) validate_billing_canary_checkout.
# ---------------------------------------------------------------------------

_CHECKOUT_TUPLE = {
    "id": "id = gen_random_uuid()",
    "builder_id": "builder_id = :other_builder",
    "intent_kind": "intent_kind = 'topup'",
    "intent_id": "intent_id = gen_random_uuid()",
    "topup_order_id": "topup_order_id = gen_random_uuid()",
    "attempt_no": "attempt_no = attempt_no + 1",
    "provider": "provider = 'g10_unknown'",
    "catalog_version_id": "catalog_version_id = 'catalog-v2'",
    "offer_id": "offer_id = 'plan_pro_monthly_v1'",
    "amount_cents": "amount_cents = amount_cents + 1",
    "pricing_snapshot": "pricing_snapshot = pricing_snapshot || '{\"x\": 1}'",
    "next_due_date": "next_due_date = next_due_date + 1",
}
_CHECKOUT_GUARDS = {
    **{
        field: (assignment, "canary checkout identity is immutable")
        for field, assignment in _CHECKOUT_TUPLE.items()
    },
    "canary_environment": (
        "canary_environment = 'staging'",
        "canary checkout provenance is immutable",
    ),
    "canary_environment_null": (
        "canary_environment = NULL, canary_slot = NULL",
        "canary checkout provenance is immutable",
    ),
    "canary_slot": ("canary_slot = 'topup'", "canary checkout provenance is immutable"),
    "external_checkout_id": (
        "external_checkout_id = 'chk_g10_rewritten'",
        "provider checkout identity is write-once",
    ),
    "external_checkout_id_null": (
        "external_checkout_id = NULL",
        "provider checkout identity is write-once",
    ),
    "resulting_external_subscription_id": (
        "resulting_external_subscription_id = 'sub_g10_rewritten'",
        "provider subscription result is write-once",
    ),
    "live_state_without_provenance": (
        "state = 'active', canary_environment = NULL",
        "active canary checkout provenance is required",
    ),
}


@pytest.mark.parametrize("field", sorted(_CHECKOUT_GUARDS))
async def test_canary_checkout_identity_tuple_is_guarded(
    trigger_harness: _TriggerHarness, canary: _Chain, general: _Chain, field: str
) -> None:
    assignment, message = _CHECKOUT_GUARDS[field]
    await _rejected(
        trigger_harness,
        f"UPDATE billing_checkout_sessions SET {assignment} WHERE id = :id",
        {"id": canary.checkout_id, "other_builder": general.builder_id},
        message=message,
        function=_CHECKOUT,
    )


async def test_canary_checkout_legit_state_writes_pass(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    """The service path already moved it ``creating -> active -> completed``
    and wrote both provider ids once (see the baseline test); here the
    remaining mutable columns and a live state with matching provenance."""

    update = "UPDATE billing_checkout_sessions SET {} WHERE id = :id"
    by_id = {"id": canary.checkout_id}
    counts, observed = await _accepted(
        trigger_harness,
        [
            (update.format("state = 'paid_pending_subscription'"), by_id),
            (update.format("state = 'completed'"), by_id),
            (
                update.format("reconciliation_attempts = reconciliation_attempts + 1"),
                by_id,
            ),
            (update.format("updated_at = updated_at + interval '1 day'"), by_id),
            (
                update.format(
                    "external_checkout_id = external_checkout_id, "
                    "resulting_external_subscription_id = "
                    "resulting_external_subscription_id"
                ),
                by_id,
            ),
        ],
        probe=("SELECT state FROM billing_checkout_sessions WHERE id = :id", by_id),
    )
    assert counts == [1] * 5 and tuple(observed) == ("completed",)


def _checkout_insert_cases(canary: _Chain, general: _Chain) -> dict[str, Any]:
    fresh = {
        "id": "gen_random_uuid()",
        "intent_id": "gen_random_uuid()",
        "external_checkout_id": "NULL",
        "resulting_external_subscription_id": "NULL",
        "state": "'creating'",
    }
    return {
        "builder_not_allowlisted": (
            {**fresh, "builder_id": f"'{general.builder_id}'::uuid"},
            "canary checkout is not DB-authorized",
        ),
        "not_creating": (
            {**fresh, "state": "'active'"},
            "canary checkout must begin creating",
        ),
        "allowlisted_builder_without_provenance": (
            {**fresh, "canary_environment": "NULL", "canary_slot": "NULL"},
            "active canary checkout provenance is required",
        ),
        # The builder IS allowlisted, but in 'development', not 'staging'; a
        # terminal state skips the live-provenance rule, so only the
        # per-environment DB authorization can refuse it.
        "allowlisted_in_another_environment": (
            {**fresh, "state": "'completed'", "canary_environment": "'staging'"},
            "canary checkout is not DB-authorized",
        ),
    }


@pytest.mark.parametrize(
    "case",
    [
        "builder_not_allowlisted",
        "not_creating",
        "allowlisted_builder_without_provenance",
        "allowlisted_in_another_environment",
    ],
)
async def test_canary_checkout_insert_rules(
    trigger_harness: _TriggerHarness, canary: _Chain, general: _Chain, case: str
) -> None:
    overrides, message = _checkout_insert_cases(canary, general)[case]
    await _rejected(
        trigger_harness,
        await _clone_sql(trigger_harness, "billing_checkout_sessions", overrides),
        {"source_id": canary.checkout_id},
        message=message,
        function=_CHECKOUT,
    )


async def test_canary_checkout_outside_its_lifetime_slot_fails_at_commit(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    """An authorized ``creating`` canary row the control never reserved.

    The BEFORE trigger admits it; the deferred slot trigger refuses at COMMIT.
    """

    harness = trigger_harness
    environment = "production"
    builder_id = await _builder(harness, "G10 slot builder")
    await _transition(harness, environment, "canary_allowlist", builder_id)
    message = "canary checkout is outside its lifetime slot"
    _EXPECTED_ERRORS[message] += 1
    insert = await _clone_sql(
        harness,
        "billing_checkout_sessions",
        {
            "id": "gen_random_uuid()",
            "intent_id": "gen_random_uuid()",
            "builder_id": f"'{builder_id}'::uuid",
            "external_checkout_id": "NULL",
            "resulting_external_subscription_id": "NULL",
            "state": "'creating'",
            "canary_environment": f"'{environment}'",
        },
    )
    async with harness.session() as session:
        try:
            result = await session.execute(
                text(insert), {"source_id": canary.checkout_id}
            )
            assert result.rowcount == 1  # type: ignore[attr-defined]
            with pytest.raises(DBAPIError, match=re.escape(message)) as info:
                await session.commit()
        finally:
            await session.rollback()
    _assert_raised_by(info.value, message=message, function=_SLOT)
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_checkout_sessions WHERE builder_id = :b",
            b=builder_id,
        )
        == 0
    )


@pytest.mark.parametrize(
    ("assignment", "message"),
    [
        (
            "external_checkout_id = 'chk_g10_rewritten'",
            "provider checkout identity is write-once",
        ),
        ("external_checkout_id = NULL", "provider checkout identity is write-once"),
        (
            "resulting_external_subscription_id = 'sub_g10_rewritten'",
            "provider subscription result is write-once",
        ),
    ],
    ids=["external_checkout_id", "external_checkout_id_null", "resulting_subscription"],
)
async def test_general_checkout_provider_ids_are_write_once(
    trigger_harness: _TriggerHarness, general: _Chain, assignment: str, message: str
) -> None:
    """The provider-id write-once rules are not canary-only."""

    await _rejected(
        trigger_harness,
        f"UPDATE billing_checkout_sessions SET {assignment} WHERE id = :id",
        {"id": general.checkout_id},
        message=message,
        function=_CHECKOUT,
    )


async def test_checkout_writes_take_the_per_builder_canary_advisory_lock(
    trigger_harness: _TriggerHarness, general: _Chain
) -> None:
    """Every checkout row write (canary or not) serializes per builder."""

    harness = trigger_harness
    holder = harness.session()
    writer = harness.session()
    try:
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'billing-canary-builder:' || CAST(:builder AS text), 0))"
            ),
            {"builder": str(general.builder_id)},
        )
        writer_pid = await _pid(writer)

        async def write() -> int:
            result = await writer.execute(
                text(
                    "UPDATE billing_checkout_sessions SET updated_at = updated_at "
                    "WHERE id = :id"
                ),
                {"id": general.checkout_id},
            )
            count = int(result.rowcount)  # type: ignore[attr-defined]
            await writer.rollback()
            return count

        task = asyncio.create_task(write())
        assert await _wait_until_blocked(harness, writer_pid, timeout=_WAIT_SECONDS)
        waiting_on = await _scalar(
            harness,
            "SELECT wait_event FROM pg_stat_activity WHERE pid = :pid",
            pid=writer_pid,
        )
        assert waiting_on == "advisory"
        assert not task.done()
        await holder.rollback()
        assert await asyncio.wait_for(task, timeout=_WAIT_SECONDS) == 1
    finally:
        await holder.close()
        await writer.close()


# ---------------------------------------------------------------------------
# (d2) The two checkout CHECKs 036 rewrites, probed in isolation.
#
# ``session_replication_role = replica`` (superuser, rolled back) disables
# every trigger -- the BEFORE validators, the deferred slot trigger and the
# foreign-key triggers -- but not CHECK constraints, so each accepted row and
# each rejection below is the CHECK's own verdict (SQLSTATE 23514 and the
# exact constraint name).
# ---------------------------------------------------------------------------

_CHECK_VIOLATION = "23514"
_CANARY_EXACT = "ck_billing_checkout_sessions_canary_exact"
_INTENT_TARGET = "ck_billing_checkout_sessions_intent_target"


def _plus_snapshot(**changes: Any) -> dict[str, Any]:
    grant = changes.pop("credit_grant_mc", 5_000_000)
    line_grant = changes.pop("line_credit_grant_mc", grant)
    snapshot: dict[str, Any] = {
        "catalog_version_id": "payment-2026-10-03",
        "offer_id": "plan_plus_monthly_v1",
        "amount_cents": 99_900,
        "credit_grant_mc": grant,
        "included_clinics": 1,
        "billing_cycle": "monthly",
        "payment_method": "credit_card",
        "lines": [
            {
                "line_id": "plan",
                "offer_id": "plan_plus_monthly_v1",
                "kind": "plan",
                "quantity": 1,
                "unit_amount_cents": 99_900,
                "line_amount_cents": 99_900,
                "credit_grant_mc": line_grant,
                "settled_cents": 0,
                "reversed_cents": 0,
                "restored_cents": 0,
                "line_financial_state": "pending",
            }
        ],
    }
    snapshot.update(changes)
    return snapshot


def _topup_snapshot(**changes: Any) -> dict[str, Any]:
    return {**json.loads(_TOPUP_SNAPSHOT), **changes}


_TOPUP_COLUMNS = {
    "intent_kind": "'topup'",
    "topup_order_id": ":intent",
    "offer_id": "'package_credits_500_v1'",
    "amount_cents": "10000",
    "canary_slot": "'topup'",
}

# name -> (column overrides, pricing snapshot, refusing constraint or None)
_CANARY_EXACT_CASES: dict[str, tuple[dict[str, str], dict[str, Any], str | None]] = {
    "subscription_grant_5000000": ({}, _plus_snapshot(), None),
    "subscription_grant_5000": ({}, _plus_snapshot(credit_grant_mc=5_000), None),
    "topup": (_TOPUP_COLUMNS, _topup_snapshot(), None),
    "non_canary_any_shape": (
        {
            "canary_environment": "NULL",
            "canary_slot": "NULL",
            "attempt_no": "2",
            "offer_id": "'plan_pro_monthly_v1'",
            "amount_cents": "12345",
            "next_due_date": "NULL",
        },
        {},
        None,
    ),
    "subscription_attempt_2": ({"attempt_no": "2"}, _plus_snapshot(), _CANARY_EXACT),
    "subscription_catalog": (
        {"catalog_version_id": "'catalog-v2'"},
        _plus_snapshot(catalog_version_id="catalog-v2"),
        _CANARY_EXACT,
    ),
    "subscription_offer": (
        {"offer_id": "'plan_pro_monthly_v1'"},
        _plus_snapshot(offer_id="plan_pro_monthly_v1"),
        _CANARY_EXACT,
    ),
    "subscription_amount": (
        {"amount_cents": "99901"},
        _plus_snapshot(amount_cents=99_901),
        _CANARY_EXACT,
    ),
    "subscription_payment_method": (
        {},
        _plus_snapshot(payment_method="pix"),
        _CANARY_EXACT,
    ),
    "subscription_snapshot_extra_key": (
        {},
        _plus_snapshot(coupon_code="G10"),
        _CANARY_EXACT,
    ),
    "subscription_grant_other": (
        {},
        _plus_snapshot(credit_grant_mc=50_000),
        _CANARY_EXACT,
    ),
    "subscription_line_grant_mismatch": (
        {},
        _plus_snapshot(credit_grant_mc=5_000, line_credit_grant_mc=5_000_000),
        _CANARY_EXACT,
    ),
    "subscription_next_due_null": (
        {"next_due_date": "NULL"},
        _plus_snapshot(),
        _CANARY_EXACT,
    ),
    "topup_amount": (
        {**_TOPUP_COLUMNS, "amount_cents": "10001"},
        _topup_snapshot(amount_cents=10_001),
        _CANARY_EXACT,
    ),
    "topup_grant": (
        _TOPUP_COLUMNS,
        _topup_snapshot(credit_grant_mc=500_001),
        _CANARY_EXACT,
    ),
    "topup_order_is_not_intent": (
        {**_TOPUP_COLUMNS, "intent_id": "gen_random_uuid()"},
        _topup_snapshot(),
        _CANARY_EXACT,
    ),
}

# name -> (column overrides, refusing constraint or None); non-canary rows.
_INTENT_TARGET_CASES: dict[str, tuple[dict[str, str], str | None]] = {
    "subscription_plain": ({}, None),
    "topup_plain": ({"intent_kind": "'topup'", "topup_order_id": ":intent"}, None),
    "subscription_with_topup_order": (
        {"topup_order_id": "gen_random_uuid()"},
        _INTENT_TARGET,
    ),
    "topup_without_order": ({"intent_kind": "'topup'"}, _INTENT_TARGET),
    "topup_order_is_not_intent": (
        {"intent_kind": "'topup'", "topup_order_id": "gen_random_uuid()"},
        _INTENT_TARGET,
    ),
}


async def _check_probe(
    harness: _Harness,
    table: str,
    sql: str,
    params: dict[str, Any],
    constraint: str | None,
) -> None:
    """Run ``sql`` with triggers off; ``constraint`` None means accepted."""

    await _require_superuser(harness)
    message = (
        None
        if constraint is None
        else f'new row for relation "{table}" violates check constraint '
        f'"{constraint}"'
    )
    if message is not None:
        _EXPECTED_ERRORS[message] += 1
    async with harness.session() as session:
        try:
            await session.execute(text(_PLANT))
            if message is None:
                result = await session.execute(text(sql), params)
                assert result.rowcount == 1  # type: ignore[attr-defined]
                return
            with pytest.raises(DBAPIError, match=re.escape(message)) as info:
                await session.execute(text(sql), params)
        finally:
            await session.rollback()
    cause = _postgres_cause(info.value)
    assert _sqlstate(info.value) == _CHECK_VIOLATION
    assert cause.message == message
    assert cause.constraint_name == constraint


_FRESH_CHECKOUT = {
    "id": "gen_random_uuid()",
    "intent_id": ":intent",
    "external_checkout_id": "NULL",
    "resulting_external_subscription_id": "NULL",
}


@pytest.mark.parametrize("case", sorted(_CANARY_EXACT_CASES))
async def test_canary_checkout_check_constraints(
    trigger_harness: _TriggerHarness, canary: _Chain, case: str
) -> None:
    overrides, snapshot, constraint = _CANARY_EXACT_CASES[case]
    sql = await _clone_sql(
        trigger_harness,
        "billing_checkout_sessions",
        {
            **_FRESH_CHECKOUT,
            # 'test' has no canary checkout: the (environment, slot) unique
            # index never interferes with the CHECK under probe.
            "canary_environment": "'test'",
            "pricing_snapshot": "CAST(:snapshot AS jsonb)",
            **overrides,
        },
    )
    await _check_probe(
        trigger_harness,
        "billing_checkout_sessions",
        sql,
        {
            "source_id": canary.checkout_id,
            "intent": uuid4(),
            "snapshot": json.dumps(snapshot),
        },
        constraint,
    )


@pytest.mark.parametrize("case", sorted(_INTENT_TARGET_CASES))
async def test_checkout_intent_target_check(
    trigger_harness: _TriggerHarness, general: _Chain, case: str
) -> None:
    overrides, constraint = _INTENT_TARGET_CASES[case]
    sql = await _clone_sql(
        trigger_harness,
        "billing_checkout_sessions",
        {**_FRESH_CHECKOUT, **overrides},
    )
    await _check_probe(
        trigger_harness,
        "billing_checkout_sessions",
        sql,
        {"source_id": general.checkout_id, "intent": uuid4()},
        constraint,
    )


async def test_asaas_subscription_is_monthly_only(
    trigger_harness: _TriggerHarness, general: _Chain
) -> None:
    """``ck_billing_subscriptions_cycle``: an ASAAS row cannot be annual,
    even where the write-once trigger is out of the way."""

    await _check_probe(
        trigger_harness,
        "billing_subscriptions",
        "UPDATE billing_subscriptions SET billing_cycle = 'annual' WHERE id = :id",
        {"id": general.subscription_id},
        "ck_billing_subscriptions_cycle",
    )


# ---------------------------------------------------------------------------
# (d3) Canary financial history is not deletable (row-level WHEN guards).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        "billing_checkout_sessions",
        "billing_payment_projections",
        "billing_payment_effects",
    ],
)
async def test_canary_financial_history_is_not_deletable(
    trigger_harness: _TriggerHarness, canary: _Chain, table: str
) -> None:
    row_id = {
        "billing_checkout_sessions": canary.checkout_id,
        "billing_payment_projections": canary.projection_id,
        "billing_payment_effects": await _effect_id(trigger_harness, canary),
    }[table]
    await _rejected(
        trigger_harness,
        f"DELETE FROM {table} WHERE id = :id",
        {"id": row_id},
        message="billing financial history is not deletable",
        function="protect_billing_financial_history",
    )


# ---------------------------------------------------------------------------
# (e) The legacy roster after migration 036.
#
# E1 pinned the 035 coupling here: every billing_subscriptions UPDATE/DELETE
# statement took the global roster fence and ASAAS rows froze
# roster_revision.  Migration 036 removed both, so these tests now prove the
# absence: holding the global advisory key or the billing_roster_control row
# no longer delays any billing_subscriptions statement, the ASAAS paths never
# touch the global revision, and the catalog keeps no roster or migration
# link on the ASAAS tables.  billing_roster_control itself (and its history
# trigger) lives on, inert, until 037.
#
# Inventory for E6 -- the machine-checkable list is the two module constants
# right below this comment (pytest node-id suffixes, parameters included);
# test_break_inventory_names_only_collected_tests proves every entry exists.
# TESTS_THAT_BREAK_AFTER_036 is empty: E5 moved every test it listed to the
# 036 behaviour (or removed the parametrizations that named a dropped column).
#
# AFTER 037 THE WHOLE MODULE ERRORS, not only the listed tests: the three
# chain fixtures ``canary``, ``general`` and ``general_paid`` call
# _roster_revision(), which reads billing_roster_control.  E6 must first edit
# those fixtures (drop roster_revision_before/after from _Chain and the
# _roster_revision() calls), then remove _roster_revision() and the tests
# listed in TESTS_THAT_BREAK_AFTER_037.
# ---------------------------------------------------------------------------

TESTS_THAT_BREAK_AFTER_036: tuple[str, ...] = ()

# Only the tests that newly break with 037; on top of these, every test of
# the module errors through the chain fixtures until E6 edits them (above).
TESTS_THAT_BREAK_AFTER_037: tuple[str, ...] = (
    # billing_roster_control and its history trigger are dropped.
    "test_legacy_roster_revision_is_saga_owned",
    "test_subscription_update_does_not_wait_on_the_roster_row_lock",
    "test_asaas_paths_never_touch_the_global_roster_revision",
    # Reads the global revision through _roster_revision().
    "test_concurrent_handoff_payment_effect_and_readback_converge",
)


async def test_break_inventory_names_only_collected_tests(
    request: pytest.FixtureRequest,
) -> None:
    """Every inventory entry is a real node id of this module (not stale)."""

    module = request.node.parent
    assert isinstance(module, pytest.Module)
    collected = {item.name for item in module.collect()}
    listed = TESTS_THAT_BREAK_AFTER_036 + TESTS_THAT_BREAK_AFTER_037
    assert len(set(listed)) == len(listed)
    assert sorted(set(listed) - collected) == []


async def _pid(session: AsyncSession) -> int:
    return int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())


# The global key 026's lock_billing_roster_statement() takes; until 036 the
# billing_subscriptions statement trigger called it (the legacy tables' own
# triggers still do, until 037 drops them).
_GLOBAL_ROSTER_FENCE_KEY = 3486705124568449331
# Long enough for a lock-free statement on a slow runner.  The 035 trigger
# waited for as long as the key was held, so a regression fails here: the
# holder is released after this window and the write is reported as blocked.
_FENCE_FREE_SECONDS = 15.0


async def _completes_while_held(
    harness: _Harness, hold: Statement, statement: Statement
) -> tuple[bool, int]:
    """Run *statement* while another session holds *hold*; never hangs.

    Returns ``(completed_while_held, rowcount)``; the writer is rolled back.
    """

    holder = harness.session()
    writer = harness.session()
    try:
        await holder.execute(text(hold[0]), hold[1])

        async def write() -> int:
            result = await writer.execute(text(statement[0]), statement[1])
            count = int(result.rowcount)  # type: ignore[attr-defined]
            await writer.rollback()
            return count

        task = asyncio.create_task(write())
        done, _ = await asyncio.wait({task}, timeout=_FENCE_FREE_SECONDS)
        # Released only now: a writer that waited on *hold* finishes after it.
        await holder.rollback()
        return bool(done), await asyncio.wait_for(task, timeout=_WAIT_SECONDS)
    finally:
        await holder.close()
        await writer.close()


@pytest.mark.parametrize(
    ("statement", "rows"),
    [
        (
            "UPDATE billing_subscriptions SET updated_at = updated_at "
            "WHERE id = :id",
            1,
        ),
        ("UPDATE billing_subscriptions SET updated_at = updated_at WHERE false", 0),
        ("DELETE FROM billing_subscriptions WHERE false", 0),
    ],
    ids=["asaas_row_update", "zero_row_update", "zero_row_delete"],
)
async def test_subscription_statements_do_not_wait_on_the_global_roster_fence(
    trigger_harness: _TriggerHarness, canary: _Chain, statement: str, rows: int
) -> None:
    """035: each of these statements queued behind a held global roster key
    (the statement trigger ``trg_billing_subscriptions_roster_fence``).  036
    dropped that trigger: they complete while the key is held."""

    completed, count = await _completes_while_held(
        trigger_harness,
        (
            "SELECT pg_advisory_xact_lock(:key)",
            {"key": _GLOBAL_ROSTER_FENCE_KEY},
        ),
        (statement, {"id": canary.subscription_id}),
    )
    assert completed, "the statement waited on the global roster key"
    assert count == rows


async def test_subscription_update_does_not_wait_on_the_roster_row_lock(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    """035: an update queued behind a ``billing_roster_control`` row lock."""

    completed, count = await _completes_while_held(
        trigger_harness,
        (
            "SELECT 1 FROM billing_roster_control WHERE singleton = true " "FOR UPDATE",
            {},
        ),
        (
            "UPDATE billing_subscriptions SET updated_at = updated_at "
            "WHERE id = :id",
            {"id": canary.subscription_id},
        ),
    )
    assert completed, "the update waited on the billing_roster_control row"
    assert count == 1


async def test_asaas_paths_never_touch_the_global_roster_revision(
    trigger_harness: _TriggerHarness,
    canary: _Chain,
    general: _Chain,
    general_paid: _Chain,
) -> None:
    """Handoff, settlement and the webhook paths leave the inert global
    revision untouched (035: they also stamped ``max(revision, 1)`` on the
    row, a column 036 dropped)."""

    for chain in (canary, general, general_paid):
        assert chain.roster_revision_after == chain.roster_revision_before
    assert await _roster_revision(trigger_harness) == canary.roster_revision_before


async def test_036_left_no_roster_or_migration_link_on_the_asaas_tables(
    trigger_harness: _TriggerHarness,
) -> None:
    harness = trigger_harness
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'public' AND (table_name, column_name) IN ("
            "('billing_subscriptions', 'roster_revision'), "
            "('billing_rollout_readiness_receipts', 'roster_revision'), "
            "('billing_checkout_sessions', 'migration_id'))",
        )
        == 0
    )
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM pg_trigger "
            "WHERE tgname = 'trg_billing_subscriptions_roster_fence'",
        )
        == 0
    )
    async with harness.session() as session:
        coupled = (
            await session.execute(
                text(
                    "SELECT proname FROM pg_proc WHERE proname = ANY(:names) "
                    "AND prosrc ~ '(billing_migration|roster|migration_id)' "
                    "ORDER BY proname"
                ),
                {"names": [_PROTECT, _PROJECTION, _CHECKOUT, _EFFECT]},
            )
        ).all()
        checks = (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = ANY(ARRAY["
                    "'billing_subscriptions'::regclass, "
                    "'billing_checkout_sessions'::regclass, "
                    "'billing_rollout_readiness_receipts'::regclass]) "
                    "AND (pg_get_constraintdef(oid) ~ '(roster|migration)' "
                    "     OR conname ~ '(roster|migration)') "
                    "ORDER BY conname"
                )
            )
        ).all()
    assert coupled == [] and checks == []


async def test_legacy_roster_revision_is_saga_owned(
    trigger_harness: _TriggerHarness, canary: _Chain
) -> None:
    # The legacy table keeps its 026 history trigger until 037 drops both.
    current = await _roster_revision(trigger_harness)
    await _rejected(
        trigger_harness,
        "UPDATE billing_roster_control SET billing_roster_revision = :next "
        "WHERE singleton = true",
        {"next": current + 2},
        message="billing roster revision is saga-owned",
        function=_ROSTER_HISTORY,
    )


# ---------------------------------------------------------------------------
# (e2) Non-ASAAS rows (abacatepay or NULL provider) under the rewritten guard.
#
# Owner decision (Group 10 plan answers, 2026-10-18): such rows stay
# representable (no provider hardening); the rewritten
# protect_billing_canary_subscription keeps their snapshot immutable --
# provider included -- and still refuses a self-asserted legacy cancellation.
# The saga that could cancel, rebase or flip them is gone, so each of those
# paths is refused now.  No production path writes such a row any more; each
# probe plants one inside its rolled-back transaction (billing_subscriptions
# has no INSERT trigger).
# ---------------------------------------------------------------------------

_LEGACY_ROWS: dict[str, tuple[str, str, str]] = {
    # provider, status, is_active -- each satisfying every CHECK.
    "abacatepay": ("'abacatepay'", "'active'", "true"),
    "null_provider": ("NULL", "'legacy_cancel_pending'", "false"),
}
_LEGACY_SNAPSHOT_IMMUTABLE = "legacy subscription snapshot is immutable"
_SELF_ASSERTED_CANCELLATION = "legacy cancellation cannot be self-asserted"


def _planted_legacy_row(kind: str, builder_id: UUID, row_id: UUID) -> Statement:
    provider, status, is_active = _LEGACY_ROWS[kind]
    return (
        "INSERT INTO billing_subscriptions (id, builder_id, provider, "
        "external_subscription_id, is_active, plan, billing_cycle, status) "
        f"VALUES (:legacy, :builder, {provider}, :external, {is_active}, "
        f"'start', 'annual', {status})",
        {
            "legacy": row_id,
            "builder": builder_id,
            "external": f"legacy-g10-{row_id.hex[:12]}",
        },
    )


_LEGACY_SNAPSHOT_MUTATIONS = {
    "provider_to_asaas": "provider = 'asaas'",
    "provider_to_other_legacy": (
        "provider = CASE WHEN provider IS NULL THEN 'abacatepay' END"
    ),
    "external_subscription_id": "external_subscription_id = 'legacy-g10-rewritten'",
    "plan": "plan = 'pro'",
    "billing_cycle": "billing_cycle = 'monthly'",
    "coupon_code": "coupon_code = 'G10COUPON'",
    "catalog_version_id": "catalog_version_id = 'payment-2026-10-03'",
    "offer_id": "offer_id = 'plan_plus_monthly_v1'",
    "amount_cents": "amount_cents = 99900",
}


@pytest.mark.parametrize("kind", sorted(_LEGACY_ROWS))
@pytest.mark.parametrize("field", sorted(_LEGACY_SNAPSHOT_MUTATIONS))
async def test_non_asaas_subscription_snapshot_is_immutable(
    trigger_harness: _TriggerHarness, general: _Chain, kind: str, field: str
) -> None:
    row_id = uuid4()
    await _rejected(
        trigger_harness,
        f"UPDATE billing_subscriptions SET {_LEGACY_SNAPSHOT_MUTATIONS[field]} "
        "WHERE id = :id",
        {"id": row_id},
        setup=[_planted_legacy_row(kind, general.builder_id, row_id)],
        message=_LEGACY_SNAPSHOT_IMMUTABLE,
        function=_PROTECT,
    )


@pytest.mark.parametrize(
    ("kind", "assignment"),
    [
        # The retired saga's entry point: AbacatePay row -> pending cancellation.
        (
            "abacatepay",
            "provider = NULL, status = 'legacy_cancel_pending', is_active = false",
        ),
        # The retired provider flip: legacy row -> its ASAAS successor.
        (
            "null_provider",
            "provider = 'asaas', status = 'active', is_active = true, "
            "billing_cycle = 'monthly', plan = 'plus', "
            "catalog_version_id = 'payment-2026-10-03', "
            "offer_id = 'plan_plus_monthly_v1', amount_cents = 99900",
        ),
    ],
    ids=["abacatepay_to_pending_cancellation", "legacy_provider_flip_to_asaas"],
)
async def test_retired_saga_transitions_are_refused(
    trigger_harness: _TriggerHarness, general: _Chain, kind: str, assignment: str
) -> None:
    row_id = uuid4()
    await _rejected(
        trigger_harness,
        f"UPDATE billing_subscriptions SET {assignment} WHERE id = :id",
        {"id": row_id},
        setup=[_planted_legacy_row(kind, general.builder_id, row_id)],
        message=_LEGACY_SNAPSHOT_IMMUTABLE,
        function=_PROTECT,
    )


@pytest.mark.parametrize(
    "assignment",
    [
        "status = 'legacy_cancelled'",
        # Checked before the snapshot: same refusal with a snapshot change.
        "status = 'legacy_cancelled', external_subscription_id = 'legacy-g10-x'",
        # ... and while leaving the NULL provider (035's second branch).
        "status = 'legacy_cancelled', provider = 'asaas'",
    ],
    ids=["status_only", "with_snapshot_change", "with_provider_change"],
)
async def test_legacy_cancellation_cannot_be_self_asserted(
    trigger_harness: _TriggerHarness, general: _Chain, assignment: str
) -> None:
    """035 refused this unless the saga held an exact cancellation receipt;
    036 has no saga, so it is always refused (same message, SQLSTATE P0001
    as in 035: that RAISE never named an error code)."""

    row_id = uuid4()
    await _rejected(
        trigger_harness,
        f"UPDATE billing_subscriptions SET {assignment} WHERE id = :id",
        {"id": row_id},
        setup=[_planted_legacy_row("null_provider", general.builder_id, row_id)],
        message=_SELF_ASSERTED_CANCELLATION,
        function=_PROTECT,
        sqlstate="P0001",
    )


@pytest.mark.parametrize(
    ("kind", "status_moves"),
    [
        ("abacatepay", ["status = 'cancelled', is_active = false"]),
        (
            "null_provider",
            ["status = 'manual_intervention'", "status = 'provider_unknown'"],
        ),
    ],
    ids=["abacatepay", "null_provider"],
)
async def test_non_asaas_subscription_status_and_timestamps_stay_mutable(
    trigger_harness: _TriggerHarness,
    general: _Chain,
    kind: str,
    status_moves: list[str],
) -> None:
    """Only the snapshot is frozen: other status moves and timestamps pass,
    and a DELETE is silently skipped like any non-canary row's (the BEFORE
    DELETE trigger returns ``NEW``, NULL on DELETE)."""

    row_id = uuid4()
    by_id = {"id": row_id}
    update = "UPDATE billing_subscriptions SET {} WHERE id = :id"
    counts, observed = await _accepted(
        trigger_harness,
        [
            _planted_legacy_row(kind, general.builder_id, row_id),
            *[(update.format(move), by_id) for move in status_moves],
            (update.format("updated_at = updated_at + interval '1 day'"), by_id),
            ("DELETE FROM billing_subscriptions WHERE id = :id", by_id),
        ],
        probe=(
            "SELECT count(*), max(status) FROM billing_subscriptions " "WHERE id = :id",
            by_id,
        ),
    )
    assert counts == [1, *[1] * len(status_moves), 1, 0]
    final_status = status_moves[-1].split("'")[1]
    assert tuple(observed) == (1, final_status)


# ---------------------------------------------------------------------------
# (f) Concurrency: handoff vs payment effect vs readback, production locks.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _holding_rollout(harness: _Harness, environment: str) -> AsyncIterator[int]:
    """Hold the environment rollout lock (the first lock of every path)."""

    async with harness.session() as session:
        try:
            pid = await _pid(session)
            await BillingRepository(session).lock_rollout_control(
                environment=environment
            )
            yield pid
        finally:
            await session.rollback()


async def _wait_for_lock_waiters(
    harness: _TriggerHarness, holder_pid: int, expected: int
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WAIT_SECONDS
    async with harness.session() as observer:
        while True:
            waiting = int(
                (
                    await observer.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = :database AND pid <> :holder "
                            "AND wait_event_type = 'Lock'"
                        ),
                        {"database": harness.database, "holder": holder_pid},
                    )
                ).scalar_one()
            )
            await observer.rollback()
            if waiting == expected:
                return
            assert loop.time() < deadline, f"{waiting} of {expected} waiters"
            await asyncio.sleep(0.02)


async def _run_contended(
    harness: _TriggerHarness, environment: str, calls: Sequence[Any]
) -> list[Any]:
    async with _holding_rollout(harness, environment) as holder_pid:
        tasks = [asyncio.create_task(call) for call in calls]
        await _wait_for_lock_waiters(harness, holder_pid, len(tasks))
        assert not any(task.done() for task in tasks)
    return list(
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=_WAIT_SECONDS
        )
    )


async def test_concurrent_handoff_payment_effect_and_readback_converge(
    trigger_harness: _TriggerHarness,
) -> None:
    harness = trigger_harness
    environment = "staging"
    external_subscription_id = "sub_g10_concurrent"
    payment_id = "pay_g10_concurrent"
    with pytest.MonkeyPatch.context() as patch:
        _configure(patch, environment, _Flag())
        builder_id = await _builder(harness, "G10 concurrent canary")
        await _transition(harness, environment, "canary_allowlist", builder_id)
        checkout_id, external_checkout_id = await _create_subscription_checkout(
            harness, environment, builder_id
        )
        paid = await _deliver(
            harness,
            _payload("CHECKOUT_PAID", {"id": external_checkout_id}),
            environment,
        )
        assert paid.outcome == "processed"
        roster_before = await _roster_revision(harness)
        intent_id, next_due = tuple(
            await _row(
                harness,
                "SELECT intent_id, next_due_date FROM billing_checkout_sessions "
                "WHERE id = :id",
                id=checkout_id,
            )
        )
        assert isinstance(next_due, date)
        reads = _Reads(
            subscriptions={
                external_subscription_id: SubscriptionReadback(
                    external_subscription_id,
                    "ACTIVE",
                    False,
                    "MONTHLY",
                    99_900,
                    next_due,
                    str(checkout_id),
                )
            },
            by_reference={
                checkout_id: (
                    ProviderRecord(
                        external_subscription_id, str(checkout_id), "ACTIVE", False
                    ),
                )
            },
        )

        async def readback_checkout() -> str:
            async with harness.session() as session:
                return await BillingReadbackService(
                    session, client_factory=reads, environment=environment
                ).readback_checkout(checkout_id)

        async def readback_subscription() -> str:
            async with harness.session() as session:
                return await BillingReadbackService(
                    session, client_factory=reads, environment=environment
                ).readback_subscription(intent_id)

        def payment(event: str) -> Any:
            return _deliver(
                harness,
                _payment(
                    event,
                    payment_id,
                    checkout=external_checkout_id,
                    subscription=external_subscription_id,
                    value="999.00",
                ),
                environment,
            )

        # Round 1: three paths race to hand the paid checkout off.
        handoff, effect, checkout_readback = await _run_contended(
            harness,
            environment,
            [
                _deliver(
                    harness,
                    _subscription_created(
                        checkout_id, external_subscription_id, 99_900
                    ),
                    environment,
                ),
                payment("PAYMENT_RECEIVED"),
                readback_checkout(),
            ],
        )
        assert not isinstance(handoff, BaseException), handoff
        assert not isinstance(effect, BaseException), effect
        assert (handoff.outcome, effect.outcome, checkout_readback) == (
            "processed",
            "processed",
            "converged",
        )

        # Round 2: subscription readback vs a second settlement event vs a
        # redelivered subscription event, on the now-settled canary row.
        sub_readback, confirmed, created_again = await _run_contended(
            harness,
            environment,
            [
                readback_subscription(),
                payment("PAYMENT_CONFIRMED"),
                _deliver(
                    harness,
                    _subscription_created(
                        checkout_id, external_subscription_id, 99_900
                    ),
                    environment,
                ),
            ],
        )
        assert not isinstance(confirmed, BaseException), confirmed
        assert not isinstance(created_again, BaseException), created_again
        assert (sub_readback, confirmed.outcome, created_again.outcome) == (
            "converged",
            "processed",
            "processed",
        )

    final = await _row(
        harness,
        "SELECT (SELECT count(*) FROM billing_subscriptions WHERE builder_id = :b), "
        "s.id, s.external_subscription_id, s.status, s.is_active, "
        "c.state, c.resulting_external_subscription_id, "
        "(SELECT count(*) FROM billing_payment_projections WHERE builder_id = :b), "
        "p.status, p.settled_cents, p.canary_environment, p.canary_slot, "
        "p.checkout_session_id, "
        "(SELECT count(*) FROM billing_payment_effects WHERE builder_id = :b), "
        "r.canary_settlement_count "
        "FROM billing_subscriptions s "
        "JOIN billing_checkout_sessions c ON c.id = :checkout "
        "JOIN billing_payment_projections p ON p.subscription_id = s.id "
        "JOIN billing_rollout_control r ON r.environment = :env "
        "WHERE s.builder_id = :b",
        b=builder_id,
        checkout=checkout_id,
        env=environment,
    )
    assert tuple(final) == (
        1,
        intent_id,
        external_subscription_id,
        "active",
        True,
        "completed",
        external_subscription_id,
        1,
        "settled",
        99_900,
        environment,
        "subscription",
        checkout_id,
        1,
        1,
    )
    assert await _roster_revision(harness) == roster_before
    assert (
        await _scalar(
            harness,
            "SELECT count(*) FROM billing_webhook_deliveries "
            "WHERE builder_id = :b AND status <> 'processed'",
            b=builder_id,
        )
        == 0
    )


# ---------------------------------------------------------------------------
# (g) Server-log hygiene.  Must stay the LAST test of the module.
# ---------------------------------------------------------------------------

_ERROR_LINE = re.compile(r"\bERROR:\s+(?P<message>.*)$")
_FORBIDDEN = re.compile(
    r"syntax error|does not exist|deadlock|invalid input|"
    r"operator does not exist|could not|unrecognized|current transaction is aborted",
    re.IGNORECASE,
)


async def test_server_log_contains_only_intentional_rejections(
    trigger_harness: _TriggerHarness,
) -> None:
    harness = trigger_harness
    # pg_current_logfile() is superuser-only by default: fail clearly, never
    # with a bare "permission denied" (see _require_superuser).
    await _require_superuser(harness)
    admin = create_async_engine(
        harness.admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as connection:
            settings_row = (
                await connection.execute(
                    text(
                        "SELECT current_setting('logging_collector'), "
                        "current_setting('log_line_prefix'), pg_current_logfile()"
                    )
                )
            ).one()
            collector, prefix, logfile = tuple(settings_row)
            if collector != "on" or "%d" not in prefix or logfile is None:
                pytest.skip(
                    "server log hygiene needs logging_collector=on and %d in "
                    f"log_line_prefix (got {collector!r}, {prefix!r})"
                )
            marker = harness.database
            content = str(
                await connection.scalar(
                    text("SELECT pg_read_file(:path)"), {"path": logfile}
                )
            )
    finally:
        await admin.dispose()
    errors = [
        match.group("message").strip()
        for line in content.splitlines()
        if marker in line and (match := _ERROR_LINE.search(line)) is not None
    ]
    # Parse/type/missing-object/deadlock errors first: they are never intended.
    assert [error for error in errors if _FORBIDDEN.search(error)] == []
    observed = Counter(errors)
    # Extra ERRORs (unintended, or an allowed message raised by another path)
    # and registered rejections that never reached the log both fail.
    assert observed - _EXPECTED_ERRORS == Counter()
    assert _EXPECTED_ERRORS - observed == Counter()
    assert _EXPECTED_ERRORS, "no negative test registered a rejection"
