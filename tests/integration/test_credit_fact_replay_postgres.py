"""``handle_billing_fact`` on real PostgreSQL (neurostack-credit-system Group 2).

Facts are presented to the real entry point over producer rows seeded in a
migrated database (``_seed_fact`` of the ASAAS worker suite, or the exact
bytes of the six contract fixtures with their outbox, delivery and handoff
rows), always under the explicit per-test waiver of the producer's
presentation guard (``presentation_preconditions_waived``) and with the
credit switch read for real from that database (config on, schema at head).
No live replay: production keeps ``transport=None``.

Proven here: inbox/ack values and their acceptance by the untouched ASAAS
reconciler; idempotency per event and per aggregate revision under replay
and concurrency; grants, reversals, restorations and the enforcement binding
of the fixtures; rejection of problems in the fact; refusal without writes
for consumer-side divergence, a closed presentation guard or an ineffective
switch; a second cycle of one period ending ``blocked``; atomicity of the
grant and the binding; the timeout; read-only reconciliation; and that no
ASAAS row but the ack changes.

Review regressions (2026-10-26): a writer whose clock lags another's cannot
refill a lot the other already expired (the builder used to stay wedged on
``LedgerInvariantError``); the largest applied reversal target survives each
reload; partial reversals then a won chargeback across transactions; facts
and debits of one builder racing; a cycle whose restored snapshot arrives
first.  Round 2: a writer ahead of the database clock (200 days, or UTC wall
time read as Sao Paulo local) is refused without ack and a ledger clock
already ahead fails closed; a won chargeback returns capacity to the lot
that really paid (a top-up, D1, or the monthly allowance, D2); crossed
clocks with concurrency never wedge.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import random
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event, text

from src.repositories import credit_fact_ack as ack_module
from src.repositories.credit import CreditRepository
from src.services import credit_fact_consumer as consumer_module
from src.services.billing_fact_worker import BillingFactWorker
from src.services.billing_outbox import (
    RETRY_EXHAUSTED_DETAIL,
    BillingFactReconciler,
    BillingFactViolation,
    PresentationPreconditionsOpen,
    payload_digest,
    validate_fact,
)
from src.services.credit_fact_consumer import (
    PINNED_BUNDLE_DIGEST,
    PINNED_SCHEMA_DIGEST,
    ConsumerContractDivergence,
    CreditFactConsumer,
    CreditSwitchOff,
    CycleIdentityConflict,
    handle_billing_fact,
)
from src.services.credit_ledger import CreditLedger, LedgerClockAhead
from src.services.credit_reconciliation import CreditReconciliation
from src.services.credit_switch import refresh_credit_switch, stop_credit_switch
from tests.integration import test_billing_fact_worker_postgres as worker_suite
from tests.integration.test_billing_fact_worker_postgres import (
    _alert_count,
    _Clock,
    _config,
    _delivery,
    _Harness,
    _rows,
    _seed_fact,
)

pytestmark = pytest.mark.integration

# The ASAAS worker suite's harness and explicit per-test waiver, as they are.
template = worker_suite.template
clean = worker_suite.clean
presentation_preconditions_waived = worker_suite.presentation_preconditions_waived

FIXTURES = Path(__file__).parents[2] / "tests/fixtures/contracts/billing_fact_v1"
CYCLE = (
    "subscription_cycle_settled",
    "subscription_cycle_reversed",
    "subscription_cycle_restored",
)
TOPUP = ("topup_payment_settled", "topup_payment_reversed", "topup_payment_restored")
DEPLOYMENT = hashlib.sha256(b"billing.fact.v1:deployment-unverified").hexdigest()
EMPTY = hashlib.sha256(b"[]").hexdigest()
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _fixture(name: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


class _DatabaseClock:
    """PostgreSQL's clock as the ledger reads it in this suite.

    The facts are dated around T0, in the past, and the ledger checks every
    writer against the database clock (more than the tolerance ahead is
    refused, more than the tolerance behind is raised).  So
    ``CreditRepository.database_now`` reads ``instant`` -- the instant the
    test acts at (``_present``, ``_position`` and ``_acting_at`` set it) --
    and the tests of the clock rule itself set ``None`` to read the real
    ``clock_timestamp()``.
    """

    instant: datetime | None = T0


_DATABASE = _DatabaseClock()


def _acting_at(instant: datetime | None) -> None:
    _DATABASE.instant = instant


@pytest.fixture(autouse=True)
def simulated_database_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    real = CreditRepository.database_now

    async def database_now(self: CreditRepository) -> datetime:
        if _DATABASE.instant is None:
            return await real(self)
        return _DATABASE.instant

    _acting_at(T0)
    monkeypatch.setattr(CreditRepository, "database_now", database_now)


@pytest.fixture
async def consumer(
    clean: _Harness,
    presentation_preconditions_waived: None,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Harness]:
    """The real entry point on this database, the switch read for real."""

    monkeypatch.setattr(
        consumer_module, "default_session_factory", lambda: clean.sessions
    )
    monkeypatch.setattr(consumer_module, "_now", lambda: T0)
    reading = await refresh_credit_switch(enabled=True, session_factory=clean.sessions)
    assert reading.effective, reading
    try:
        yield clean
    finally:
        await stop_credit_switch(None)


async def _exec(harness: _Harness, sql: str, **params: Any) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(text(sql), params)


async def _one(harness: _Harness, sql: str, **params: Any) -> Any:
    return (await _rows(harness, sql, **params))[0][0]


async def _ensure_builder(harness: _Harness, builder_id: str) -> None:
    await _exec(
        harness,
        "INSERT INTO builders (id, name) VALUES (:id, 'Credit Replay') "
        "ON CONFLICT (id) DO NOTHING",
        id=builder_id,
    )


async def _ensure_aggregate(harness: _Harness, payload: dict[str, Any]) -> None:
    """The ASAAS aggregate rows the outbox foreign keys name (revision 0)."""

    builder = payload["builder_id"]
    await _ensure_builder(harness, builder)
    if payload["aggregate_kind"] == "topup_order":
        snapshot = {
            "catalog_version_id": payload["catalog_version_id"],
            "offer_id": payload["offer_id"],
        }
        await _exec(
            harness,
            """
            INSERT INTO billing_topup_orders (
                id, builder_id, payment_method, catalog_version_id, offer_id,
                amount_cents, credit_grant_mc, pricing_snapshot
            ) VALUES (
                :id, :builder, 'pix', :catalog, :offer, :amount, :grant,
                CAST(:snapshot AS JSONB)
            ) ON CONFLICT (id) DO NOTHING
            """,
            id=payload["aggregate_id"],
            builder=builder,
            catalog=payload["catalog_version_id"],
            offer=payload["offer_id"],
            amount=payload["total_amount_cents"],
            grant=payload["credit_grant_mc"],
            snapshot=json.dumps(snapshot),
        )
        return
    await _exec(
        harness,
        """
        INSERT INTO billing_subscriptions (
            id, builder_id, provider, external_subscription_id, is_active, plan,
            billing_cycle, status
        ) VALUES (:id, :builder, 'asaas', :external, true, 'plus', 'monthly', 'active')
        ON CONFLICT (id) DO NOTHING
        """,
        id=payload["subscription_id"],
        builder=builder,
        external=f"sub_{payload['subscription_id'][:8]}",
    )
    period = payload["subscription_cycle"]
    await _exec(
        harness,
        """
        INSERT INTO billing_subscription_cycles (
            id, builder_id, subscription_id, payment_id, catalog_version_id,
            offer_id, cycle_start, cycle_end, amount_cents, credit_grant_mc,
            pricing_snapshot, financial_snapshot
        ) VALUES (
            :id, :builder, :subscription, :payment, :catalog, :offer,
            :start, :end, :amount, :grant, '{}'::jsonb, '{}'::jsonb
        ) ON CONFLICT (id) DO NOTHING
        """,
        id=payload["aggregate_id"],
        builder=builder,
        subscription=payload["subscription_id"],
        payment=payload["payment_id"],
        catalog=payload["catalog_version_id"],
        offer=payload["offer_id"],
        start=date.fromisoformat(period["cycle_start"]),
        end=date.fromisoformat(period["cycle_end"]),
        amount=payload["total_amount_cents"],
        grant=payload["credit_grant_mc"],
    )


async def _lineage(connection: Any, payload: dict[str, Any]) -> UUID:
    """Webhook delivery, projection and payment effect of a top-up fact (what
    ``_seed_fact`` builds before the producer runs), so the ASAAS reconciler
    reads the ack instead of reporting missing lineage."""

    delivery_id, projection_id, effect_id = uuid4(), uuid4(), uuid4()
    builder, payment = payload["builder_id"], payload["payment_id"]
    await connection.execute(
        text("""
            INSERT INTO billing_webhook_deliveries (
                id, builder_id, provider, event_id, event_name,
                provider_created_at, body_bytes, body_sha256
            ) VALUES (:id, :builder, 'asaas', :event, 'PAYMENT_RECEIVED', now(), 2, :sha)
        """),
        {
            "id": delivery_id,
            "builder": builder,
            "event": f"lineage-{delivery_id}",
            "sha": "a" * 64,
        },
    )
    await connection.execute(
        text("""
            INSERT INTO billing_payment_projections (
                id, builder_id, provider, payment_id, aggregate_kind, topup_order_id,
                status, amount_cents, settled_cents, paid_at, provider_created_at,
                ordering_key, last_event_id
            ) VALUES (
                :id, :builder, 'asaas', :payment, 'topup', :order, 'settled',
                :amount, :settled, :paid_at, now(), '2026-10-12T00:00:00Z:0', :event
            )
        """),
        {
            "id": projection_id,
            "builder": builder,
            "payment": payment,
            "order": payload["aggregate_id"],
            "amount": payload["total_amount_cents"],
            "settled": payload["settled_cents"],
            "paid_at": date.fromisoformat(payload["paid_at"]),
            "event": f"lineage-{delivery_id}",
        },
    )
    await connection.execute(
        text("""
            INSERT INTO billing_payment_effects (
                id, builder_id, projection_id, delivery_id, provider, payment_id,
                effect_kind, source_revision, amount_cents
            ) VALUES (
                :id, :builder, :projection, :delivery, 'asaas', :payment,
                'settlement', 'settlement:v1', :amount
            )
        """),
        {
            "id": effect_id,
            "builder": builder,
            "projection": projection_id,
            "delivery": delivery_id,
            "payment": payment,
            "amount": payload["settled_cents"],
        },
    )
    return effect_id


async def _seed_payload(
    harness: _Harness, payload: dict[str, Any], *, lineage: bool = False
) -> UUID:
    """Outbox, delivery and handoff of these exact bytes (producer-owned
    rows, inserted directly: the consumer only reads them)."""

    await _ensure_aggregate(harness, payload)
    cycle = payload["aggregate_kind"] == "subscription_cycle"
    outbox_id = UUID(payload["event_id"])
    async with harness.engine.begin() as connection:
        effect_id = await _lineage(connection, payload) if lineage else None
        await connection.execute(
            text("""
                INSERT INTO billing_outbox (
                    id, builder_id, schema_version, event_type, aggregate_kind,
                    aggregate_id, subscription_id, topup_order_id,
                    subscription_cycle_id, payment_effect_id, financial_revision,
                    payload, payload_sha256, bundle_digest, deployment_digest
                ) VALUES (
                    :id, :builder, 'billing.fact.v1', :event_type, :kind,
                    :aggregate, :subscription, :order, :cycle, :effect, :revision,
                    CAST(:payload AS JSONB), :sha, :bundle, :deployment
                )
            """),
            {
                "id": outbox_id,
                "builder": payload["builder_id"],
                "event_type": payload["event_type"],
                "kind": payload["aggregate_kind"],
                "aggregate": payload["aggregate_id"],
                "subscription": payload["subscription_id"] if cycle else None,
                "order": None if cycle else payload["aggregate_id"],
                "cycle": payload["aggregate_id"] if cycle else None,
                "effect": effect_id,
                "revision": payload["financial_revision"],
                "payload": json.dumps(payload),
                "sha": payload_digest(payload),
                "bundle": PINNED_BUNDLE_DIGEST,
                "deployment": DEPLOYMENT,
            },
        )
        delivery_id = uuid4()
        await connection.execute(
            text(
                "INSERT INTO billing_fact_deliveries (id, outbox_event_id, event_id, "
                "state) VALUES (:id, :outbox, :outbox, 'pending')"
            ),
            {"id": delivery_id, "outbox": outbox_id},
        )
        await connection.execute(
            text(
                "INSERT INTO billing_fact_handoffs (id, outbox_event_id, delivery_id, "
                "schema_version, bundle_digest, deployment_digest, handed_off_at) "
                "VALUES (:id, :outbox, :delivery, 'billing.fact.v1', :bundle, "
                ":deployment, now())"
            ),
            {
                "id": uuid4(),
                "outbox": outbox_id,
                "delivery": delivery_id,
                "bundle": PINNED_BUNDLE_DIGEST,
                "deployment": DEPLOYMENT,
            },
        )
    return outbox_id


async def _asaas_state(harness: _Harness) -> dict[str, str]:
    """A digest of every ASAAS table except the consumer-owned acks."""

    tables = [
        row[0]
        for row in await _rows(
            harness,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name LIKE 'billing\\_%' "
            "AND table_name <> 'billing_fact_acks' ORDER BY table_name",
        )
    ]
    state = {}
    for table in tables:
        state[table] = await _one(
            harness,
            f"SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            f"FROM {table} t",
        )
    return state


async def _ledger(harness: _Harness, builder_id: str) -> dict[str, int]:
    rows = await _rows(
        harness,
        "SELECT entry_type, sum(amount_mc) FROM credit_ledger_entries "
        "WHERE builder_id = :b GROUP BY entry_type",
        b=builder_id,
    )
    return {str(kind): int(total) for kind, total in rows}


def _variant(payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    copied = copy.deepcopy(payload)
    copied.update(changes)
    return copied


def _full_chargeback(settled: dict[str, Any], revision: int) -> dict[str, Any]:
    reversed_ = _variant(
        settled,
        event_id=str(uuid4()),
        financial_revision=revision,
        event_type=settled["event_type"].replace("settled", "reversed"),
    )
    for line in reversed_["lines"]:
        line["reversed_cents"] = line["settled_cents"]
        line["financial_state"] = "reversed"
    reversed_["reversed_cents"] = reversed_["settled_cents"]
    return reversed_


def _won(reversed_: dict[str, Any], revision: int) -> dict[str, Any]:
    restored = _variant(
        reversed_,
        event_id=str(uuid4()),
        financial_revision=revision,
        event_type=reversed_["event_type"].replace("reversed", "restored"),
    )
    for line in restored["lines"]:
        line["restored_cents"] = line["reversed_cents"]
        line["financial_state"] = "restored"
    restored["restored_cents"] = restored["reversed_cents"]
    return restored


def _fresh_aggregate(payload: dict[str, Any], **ids: str) -> dict[str, Any]:
    copied = copy.deepcopy(payload)
    builder = ids.get("builder_id", str(uuid4()))
    aggregate = ids.get("aggregate_id", str(uuid4()))
    copied.update(event_id=str(uuid4()), builder_id=builder, aggregate_id=aggregate)
    if copied["aggregate_kind"] == "topup_order":
        copied["order_id"] = aggregate
    else:
        copied["subscription_id"] = ids.get("subscription_id", str(uuid4()))
    copied["payment_id"] = ids.get("payment_id", f"pay_{aggregate[:8]}")
    return copied


# ---------------------------------------------------------------------------
# The six fixtures, byte for byte.
# ---------------------------------------------------------------------------


async def test_the_six_fixtures_replay_exactly_once_with_their_binding(
    consumer: _Harness,
) -> None:
    payloads = [_fixture(name) for name in CYCLE + TOPUP]
    for payload in payloads:
        await _seed_payload(consumer, payload)
    asaas_before = await _asaas_state(consumer)
    for payload in payloads:
        await handle_billing_fact(payload)
    for payload in payloads:  # replay: nothing new
        await handle_billing_fact(payload)
    await asyncio.gather(*(handle_billing_fact(p) for p in payloads))
    assert await _asaas_state(consumer) == asaas_before  # only acks were written

    inbox = await _rows(
        consumer,
        "SELECT event_id, outcome, aggregate_kind, aggregate_id, financial_revision, "
        "fact_sha256, bundle_digest, schema_digest, id FROM credit_fact_inbox",
    )
    acks = {
        row[0]: row
        for row in await _rows(
            consumer,
            "SELECT event_id, outcome, schema_version, inbox_event_id, fact_sha256, "
            "bundle_digest, deployment_digest, effects_digest, outbox_event_id "
            "FROM billing_fact_acks",
        )
    }
    assert len(inbox) == len(acks) == 6
    for row in inbox:
        payload = next(p for p in payloads if UUID(p["event_id"]) == row[0])
        ack = acks[row[0]]
        assert row[1] == ack[1] == "accepted"
        assert (row[2], str(row[3]), row[4]) == (
            payload["aggregate_kind"],
            payload["aggregate_id"],
            payload["financial_revision"],
        )
        assert row[5] == ack[4] == payload_digest(payload)
        assert row[6] == ack[5] == PINNED_BUNDLE_DIGEST
        assert row[7] == PINNED_SCHEMA_DIGEST
        assert ack[2] == "billing.fact.ack.v1" and ack[3] == row[8]
        assert ack[6] == DEPLOYMENT and ack[8] == row[0]

    builder = payloads[0]["builder_id"]
    assert await _ledger(consumer, builder) == {
        "grant": 5_500_000,
        "reversal": 250_000,
        "lot_regrant": 125_000,
    }
    lots = dict(
        await _rows(
            consumer,
            "SELECT lot_kind, expires_at FROM credit_lots WHERE builder_id = :b",
            b=builder,
        )
    )
    assert lots == {
        "monthly": datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc),
        "topup": datetime(2027, 4, 12, 3, 0, tzinfo=timezone.utc),
    }
    (binding,) = await _rows(
        consumer,
        "SELECT status, financial_revision, subscription_cycle_id, cycle_start, "
        "cycle_end, grant_effect_id FROM credit_enforcement_binding",
    )
    assert binding[:5] == (
        "active",
        3,
        UUID(payloads[0]["aggregate_id"]),
        date(2026, 10, 12),
        date(2026, 11, 12),
    )
    assert binding[5].startswith("cycle:50000000-0000-4000-8000-000000000001:")
    async with consumer.sessions() as session:
        book = await CreditLedger(session).open_book(UUID(builder), at=T0)
        assert book.available_mc(T0) == 5_000_000 + 375_000
        assert book.open_debt_mc == 0
        await session.rollback()
    # Each topup step's effects digest names exactly what it wrote.
    topup_acks = [acks[UUID(p["event_id"])][7] for p in payloads[3:]]
    assert EMPTY not in topup_acks
    cycle_acks = [acks[UUID(p["event_id"])][7] for p in payloads[1:3]]
    assert cycle_acks == [EMPTY, EMPTY]  # the clinic-only steps changed no credit


async def test_out_of_order_revisions_open_and_close_a_gap(consumer: _Harness) -> None:
    payloads = [_fixture(name) for name in TOPUP]
    for payload in payloads:
        await _seed_payload(consumer, payload)
    await handle_billing_fact(payloads[0])
    await handle_billing_fact(payloads[2])
    assert await _rows(
        consumer,
        "SELECT first_missing_revision, closed_at IS NULL FROM credit_revision_gaps",
    ) == [(2, True)]
    await handle_billing_fact(payloads[1])  # older than the applied revision: no-op
    assert await _rows(
        consumer, "SELECT closed_at IS NULL FROM credit_revision_gaps"
    ) == [(False,)]
    assert await _ledger(consumer, payloads[0]["builder_id"]) == {
        "grant": 500_000,
        "reversal": 250_000,
        "lot_regrant": 125_000,
    }
    assert (
        await _one(
            consumer,
            "SELECT effects_digest FROM billing_fact_acks WHERE event_id = :e",
            e=payloads[1]["event_id"],
        )
        == EMPTY
    )


# ---------------------------------------------------------------------------
# The untouched ASAAS side reads the ack.
# ---------------------------------------------------------------------------


async def test_the_untouched_reconciler_accepts_the_ack_of_a_produced_fact(
    consumer: _Harness,
) -> None:
    outbox_id = await _seed_fact(consumer)
    (payload,) = (
        row[0]
        for row in await _rows(
            consumer, "SELECT payload FROM billing_outbox WHERE id = :id", id=outbox_id
        )
    )
    await handle_billing_fact(dict(payload))
    async with consumer.sessions() as session, session.begin():
        result = await BillingFactReconciler(session).reconcile()
    assert not result.rollout_blocked
    assert await _delivery(consumer, outbox_id) == ("acknowledged", 0)
    (ack,) = await _rows(
        consumer,
        "SELECT a.fact_sha256 = o.payload_sha256, a.bundle_digest = h.bundle_digest, "
        "a.deployment_digest = h.deployment_digest, a.outcome "
        "FROM billing_fact_acks a JOIN billing_outbox o ON o.id = a.outbox_event_id "
        "JOIN billing_fact_handoffs h ON h.outbox_event_id = a.outbox_event_id",
    )
    assert tuple(ack) == (True, True, True, "accepted")
    async with consumer.sessions() as session:
        assert (await CreditReconciliation(session).report()).ready


async def test_effect_alert_closes_when_its_effect_gets_an_accepted_fact(
    consumer: _Harness,
) -> None:
    """An effect-only alert is closed by the accepted fact for that effect."""

    outbox_id = await _seed_fact(consumer)
    ((effect_id,),) = await _rows(
        consumer,
        "SELECT payment_effect_id FROM billing_outbox WHERE id = :id",
        id=outbox_id,
    )
    digest = hashlib.sha256(f"effect:{effect_id}".encode("utf-8")).hexdigest()

    async with consumer.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_fact_reconciliation_alerts
                    (id, outbox_event_id, kind, detail_sha256, observed_at)
                VALUES (:id, NULL, 'gap', :digest, now())
            """),
            {"id": uuid4(), "digest": digest},
        )
    async with consumer.sessions() as session:
        assert (await CreditReconciliation(session).report()).open_alerts == 1

    (payload,) = (
        row[0]
        for row in await _rows(
            consumer, "SELECT payload FROM billing_outbox WHERE id = :id", id=outbox_id
        )
    )
    await handle_billing_fact(dict(payload))
    assert await _rows(
        consumer,
        "SELECT payment_effect_id FROM billing_outbox o JOIN billing_fact_acks a "
        "ON a.outbox_event_id = o.id WHERE o.id = :id AND a.outcome = 'accepted'",
        id=outbox_id,
    ) == [(effect_id,)]

    async with consumer.engine.begin() as connection:
        # Re-alerting the same repaired effect must remain closed.
        await connection.execute(
            text("""
                INSERT INTO billing_fact_reconciliation_alerts
                    (id, outbox_event_id, kind, detail_sha256, observed_at)
                VALUES (:id, NULL, 'gap', :digest, now())
            """),
            {"id": uuid4(), "digest": digest},
        )
    async with consumer.sessions() as session:
        assert (await CreditReconciliation(session).report()).open_alerts == 0

    # A digest that cannot name an effect is deliberately never inferred closed.
    malformed_digest = hashlib.sha256(b"effect:not-a-payment-effect").hexdigest()
    async with consumer.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_fact_reconciliation_alerts
                    (id, outbox_event_id, kind, detail_sha256, observed_at)
                VALUES (:id, NULL, 'gap', :digest, now())
            """),
            {"id": uuid4(), "digest": malformed_digest},
        )
    async with consumer.sessions() as session:
        assert (await CreditReconciliation(session).report()).open_alerts == 1


async def test_effect_alert_closes_on_accepted_successor_revision_only(
    consumer: _Harness,
) -> None:
    """A real no-outbox effect closes only after a later aggregate revision."""

    outbox_id = await _seed_fact(consumer)
    ((prior_effect_id,),) = await _rows(
        consumer,
        "SELECT payment_effect_id FROM billing_outbox WHERE id = :id",
        id=outbox_id,
    )
    (prior_payload,) = (
        row[0]
        for row in await _rows(
            consumer, "SELECT payload FROM billing_outbox WHERE id = :id", id=outbox_id
        )
    )
    await handle_billing_fact(dict(prior_payload))

    missing_effect, missing_delivery = uuid4(), uuid4()
    async with consumer.engine.begin() as connection:
        # This is the producer's actual alert state: a payment effect exists,
        # shares the aggregate's projection, and has no billing_outbox row.
        await connection.execute(
            text("""
                INSERT INTO billing_webhook_deliveries (
                    id, builder_id, provider, event_id, event_name,
                    provider_created_at, body_bytes, body_sha256
                )
                SELECT :delivery, builder_id, 'asaas', :event, 'PAYMENT_RECEIVED',
                       now(), 2, :sha
                FROM billing_payment_effects WHERE id = :effect
            """),
            {
                "delivery": missing_delivery,
                "event": f"missing-{missing_delivery}",
                "sha": "c" * 64,
                "effect": prior_effect_id,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_effects (
                    id, builder_id, projection_id, delivery_id, provider, payment_id,
                    effect_kind, source_revision, amount_cents
                )
                SELECT :id, builder_id, projection_id, :delivery, provider, payment_id,
                       effect_kind, source_revision || '-missing', amount_cents
                FROM billing_payment_effects WHERE id = :effect
            """),
            {
                "id": missing_effect,
                "delivery": missing_delivery,
                "effect": prior_effect_id,
            },
        )
        digest = hashlib.sha256(f"effect:{missing_effect}".encode("utf-8")).hexdigest()
        await connection.execute(
            text("""
                INSERT INTO billing_fact_reconciliation_alerts
                    (id, outbox_event_id, kind, detail_sha256, observed_at)
                VALUES (:id, NULL, 'gap', :digest, now())
            """),
            {"id": uuid4(), "digest": digest},
        )

    # An accepted fact for another aggregate cannot close this effect alert.
    other_outbox_id = await _seed_fact(consumer)
    (other_payload,) = (
        row[0]
        for row in await _rows(
            consumer,
            "SELECT payload FROM billing_outbox WHERE id = :id",
            id=other_outbox_id,
        )
    )
    await handle_billing_fact(dict(other_payload))
    async with consumer.sessions() as session:
        assert (await CreditReconciliation(session).report()).open_alerts == 1

    successor_id, successor_effect, successor_delivery = uuid4(), uuid4(), uuid4()
    successor_handoff, successor_ack = uuid4(), uuid4()
    async with consumer.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_webhook_deliveries (
                    id, builder_id, provider, event_id, event_name,
                    provider_created_at, body_bytes, body_sha256
                )
                SELECT :delivery, e.builder_id, 'asaas', :event, 'PAYMENT_RECEIVED',
                       now(), 2, :sha
                FROM billing_payment_effects e WHERE e.id = :effect
            """),
            {
                "delivery": successor_delivery,
                "event": f"successor-{successor_delivery}",
                "sha": "b" * 64,
                "effect": missing_effect,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_effects (
                    id, builder_id, projection_id, delivery_id, provider, payment_id,
                    effect_kind, source_revision, amount_cents
                )
                SELECT :id, builder_id, projection_id, :delivery, provider, payment_id,
                       effect_kind, source_revision || '-successor', amount_cents
                FROM billing_payment_effects WHERE id = :effect
            """),
            {
                "id": successor_effect,
                "delivery": successor_delivery,
                "effect": missing_effect,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_outbox (
                    id, builder_id, schema_version, event_type, aggregate_kind,
                    aggregate_id, subscription_id, topup_order_id,
                    subscription_cycle_id, payment_effect_id, financial_revision,
                    payload, payload_sha256, bundle_digest, deployment_digest
                )
                SELECT :id, builder_id, schema_version, event_type, aggregate_kind,
                       aggregate_id, subscription_id, topup_order_id,
                       subscription_cycle_id, :effect, financial_revision + 2,
                       payload, payload_sha256, bundle_digest, deployment_digest
                FROM billing_outbox WHERE id = :source
            """),
            {"id": successor_id, "effect": successor_effect, "source": outbox_id},
        )
        await connection.execute(
            text("""
                INSERT INTO billing_fact_deliveries (id, outbox_event_id, event_id, state)
                VALUES (:id, :outbox, :outbox, 'acknowledged')
            """),
            {"id": successor_delivery, "outbox": successor_id},
        )
        await connection.execute(
            text("""
                INSERT INTO billing_fact_handoffs (
                    id, outbox_event_id, delivery_id, schema_version, bundle_digest,
                    deployment_digest, handed_off_at
                )
                SELECT :id, :outbox, :delivery, schema_version, bundle_digest,
                       deployment_digest, now()
                FROM billing_fact_handoffs WHERE outbox_event_id = :source
            """),
            {
                "id": successor_handoff,
                "outbox": successor_id,
                "delivery": successor_delivery,
                "source": outbox_id,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_fact_acks (
                    id, outbox_event_id, event_id, schema_version, outcome,
                    inbox_event_id, fact_sha256, bundle_digest, deployment_digest,
                    effects_digest, acknowledged_at
                )
                SELECT :id, :outbox, :outbox, 'billing.fact.ack.v1', 'accepted',
                       :inbox, o.payload_sha256, h.bundle_digest,
                       h.deployment_digest, :effects, now()
                FROM billing_outbox o
                JOIN billing_fact_handoffs h ON h.outbox_event_id = o.id
                WHERE o.id = :outbox
            """),
            {
                "id": successor_ack,
                "outbox": successor_id,
                "inbox": uuid4(),
                "effects": EMPTY,
            },
        )
    async with consumer.sessions() as session:
        assert (await CreditReconciliation(session).report()).open_alerts == 0

    # Re-alerting remains closed after the valid successor reconciliation.
    async with consumer.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_fact_reconciliation_alerts
                    (id, outbox_event_id, kind, detail_sha256, observed_at)
                VALUES (:id, NULL, 'gap', :digest, now())
            """),
            {"id": uuid4(), "digest": digest},
        )
    async with consumer.sessions() as session:
        assert (await CreditReconciliation(session).report()).open_alerts == 0


async def test_the_worker_presents_through_the_handler(consumer: _Harness) -> None:
    outbox_id = await _seed_fact(consumer)
    clock = _Clock()
    engine, sessions = consumer.factory()
    worker = BillingFactWorker(
        sessions, transport=handle_billing_fact, config=_config(), now=clock
    )
    try:
        first = await worker.run_tick()
        clock.advance(minutes=2)
        second = await worker.run_tick()
    finally:
        await engine.dispose()
    assert (first.presented, first.failed, second.presented) == (1, 0, 0)
    assert await _delivery(consumer, outbox_id) == ("acknowledged", 1)


async def test_without_the_waiver_or_the_switch_nothing_is_written(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox_id = await _seed_fact(clean)
    (payload,) = (
        row[0]
        for row in await _rows(
            clean, "SELECT payload FROM billing_outbox WHERE id = :id", id=outbox_id
        )
    )
    monkeypatch.setattr(
        consumer_module, "default_session_factory", lambda: clean.sessions
    )
    with pytest.raises(PresentationPreconditionsOpen):
        await handle_billing_fact(dict(payload))
    with pytest.raises(PresentationPreconditionsOpen):
        BillingFactWorker(
            clean.sessions, transport=handle_billing_fact, config=_config()
        )
    from src.services import billing_outbox as outbox_module

    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", False)
    await refresh_credit_switch(enabled=False, session_factory=clean.sessions)
    with pytest.raises(CreditSwitchOff):
        await handle_billing_fact(dict(payload))
    assert await _one(clean, "SELECT count(*) FROM credit_fact_inbox") == 0
    assert await _one(clean, "SELECT count(*) FROM billing_fact_acks") == 0
    assert await _one(clean, "SELECT count(*) FROM credit_accounts") == 0


# ---------------------------------------------------------------------------
# Problems in the fact vs. problems of the consumer.
# ---------------------------------------------------------------------------


async def test_a_tampered_payload_is_rejected_and_the_reconciler_blocks_it(
    consumer: _Harness,
) -> None:
    outbox_id = await _seed_fact(consumer)  # produced by the real producer
    (stored,) = (
        row[0]
        for row in await _rows(
            consumer, "SELECT payload FROM billing_outbox WHERE id = :id", id=outbox_id
        )
    )
    payload = dict(stored)
    tampered = _variant(payload, credit_grant_mc=4_000_000)
    tampered["lines"] = copy.deepcopy(payload["lines"])
    tampered["lines"][0]["credit_grant_mc"] = 4_000_000
    await handle_billing_fact(tampered)
    await handle_billing_fact(tampered)  # replay: the same single row
    (row,) = await _rows(
        consumer,
        "SELECT i.outcome, a.outcome, a.effects_digest, a.fact_sha256, i.aggregate_id "
        "FROM credit_fact_inbox i JOIN billing_fact_acks a ON a.inbox_event_id = i.id",
    )
    assert tuple(row) == (
        "rejected",
        "rejected",
        EMPTY,
        payload_digest(tampered),
        UUID(payload["aggregate_id"]),
    )
    assert await _one(consumer, "SELECT count(*) FROM credit_effects") == 0
    async with consumer.sessions() as session, session.begin():
        await BillingFactReconciler(session).reconcile()
    assert (await _delivery(consumer, outbox_id))[0] == "blocked"
    async with consumer.sessions() as session:
        report = await CreditReconciliation(session).report()
    assert report.rejected_open == 1 and not report.ready


@pytest.mark.parametrize(
    "violation", ["partial_credit_line", "ambiguous_line_allocation"]
)
async def test_closed_invariant_violations_are_rejected_with_zero_effects(
    consumer: _Harness, violation: str
) -> None:
    payload = _fixture(TOPUP[0])
    (line,) = payload["lines"]
    if violation == "partial_credit_line":
        # A credit-bearing line only partly settled.
        line["settled_cents"] = payload["settled_cents"] = 5_000
    else:
        # Money on the envelope that no line accounts for.
        line["settled_cents"], line["financial_state"] = 0, "pending"
    with pytest.raises(BillingFactViolation):
        validate_fact(payload)
    outbox_id = await _seed_payload(consumer, payload, lineage=True)
    await handle_billing_fact(payload)
    await handle_billing_fact(payload)
    assert await _rows(
        consumer, "SELECT outcome, effects_digest FROM billing_fact_acks"
    ) == [("rejected", EMPTY)]
    assert await _one(consumer, "SELECT count(*) FROM credit_ledger_entries") == 0
    assert await _one(consumer, "SELECT count(*) FROM credit_enforcement_binding") == 0
    async with consumer.sessions() as session, session.begin():
        await BillingFactReconciler(session).reconcile()
    assert (await _delivery(consumer, outbox_id))[0] == "blocked"
    async with consumer.sessions() as session:
        assert not (await CreditReconciliation(session).report()).ready


async def test_a_consumer_side_divergence_writes_nothing_until_fixed(
    consumer: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _fixture(TOPUP[0])
    await _seed_payload(consumer, payload)

    def unavailable() -> str:
        raise BillingFactViolation("billing fact bundle manifest is unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(consumer_module, "billing_fact_bundle_digest", unavailable)
        with pytest.raises(ConsumerContractDivergence):
            await handle_billing_fact(payload)
        patched.setattr(consumer_module, "billing_fact_bundle_digest", lambda: "e" * 64)
        with pytest.raises(ConsumerContractDivergence):
            await handle_billing_fact(payload)
    assert await _one(consumer, "SELECT count(*) FROM credit_fact_inbox") == 0
    assert await _one(consumer, "SELECT count(*) FROM billing_fact_acks") == 0
    await handle_billing_fact(payload)
    assert await _rows(consumer, "SELECT outcome FROM billing_fact_acks") == [
        ("accepted",)
    ]


# ---------------------------------------------------------------------------
# Binding: atomic with the grant, one per period, follows the plan line.
# ---------------------------------------------------------------------------


async def test_the_binding_and_the_grant_roll_back_together(
    consumer: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _fixture(CYCLE[0])
    await _seed_payload(consumer, payload)

    async def lost_ack(*_args: Any, **_kwargs: Any) -> None:
        raise ConnectionError("injected failure after the binding")

    with monkeypatch.context() as patched:
        patched.setattr(ack_module.CreditFactAckRepository, "insert_ack", lost_ack)
        with pytest.raises(ConnectionError):
            await handle_billing_fact(payload)
    for table in (
        "credit_enforcement_binding",
        "credit_lots",
        "credit_effects",
        "credit_fact_inbox",
    ):
        assert await _one(consumer, f"SELECT count(*) FROM {table}") == 0, table
    await handle_billing_fact(payload)
    assert await _one(consumer, "SELECT count(*) FROM credit_enforcement_binding") == 1


async def test_a_second_cycle_of_one_period_ends_blocked_without_ack_or_binding(
    consumer: _Harness,
) -> None:
    first = _fixture(CYCLE[0])
    second = _variant(
        first,
        event_id=str(uuid4()),
        aggregate_id=str(uuid4()),
        payment_id="pay_subscription_second",
    )
    await _seed_payload(consumer, first)
    await handle_billing_fact(first)
    duplicate = await _seed_payload(consumer, second)
    with pytest.raises(CycleIdentityConflict):
        await handle_billing_fact(second)

    clock = _Clock()
    engine, sessions = consumer.factory()
    worker = BillingFactWorker(
        sessions,
        transport=handle_billing_fact,
        config=_config(max_attempts=2),
        now=clock,
    )
    try:
        outcomes = []
        for _ in range(4):
            report = await worker.run_tick()
            outcomes.append((report.failed, report.exhausted))
            clock.advance(hours=1)
    finally:
        await engine.dispose()
    assert outcomes == [(1, 0), (1, 0), (0, 1), (0, 0)]
    assert await _delivery(consumer, duplicate) == ("blocked", 2)
    assert await _alert_count(consumer, duplicate, RETRY_EXHAUSTED_DETAIL) == 1
    assert (
        await _one(
            consumer,
            "SELECT count(*) FROM billing_fact_acks WHERE outbox_event_id = :o",
            o=duplicate,
        )
        == 0
    )
    assert await _rows(
        consumer, "SELECT subscription_cycle_id FROM credit_enforcement_binding"
    ) == [(UUID(first["aggregate_id"]),)]
    async with consumer.sessions() as session:
        report = await CreditReconciliation(session).report()
    assert report.facts_without_ack == 1 and not report.ready


async def test_a_plan_chargeback_reverses_and_restores_the_binding(
    consumer: _Harness,
) -> None:
    settled = _fresh_aggregate(_fixture(CYCLE[0]))
    reversed_ = _full_chargeback(settled, 2)
    restored = _won(reversed_, 3)
    statuses = []
    for payload in (settled, reversed_, restored):
        await _seed_payload(consumer, payload)
        await handle_billing_fact(payload)
        statuses.append(
            tuple(
                (
                    await _rows(
                        consumer,
                        "SELECT status, financial_revision "
                        "FROM credit_enforcement_binding",
                    )
                )[0]
            )
        )
    assert statuses == [("active", 1), ("reversed", 2), ("active", 3)]
    assert await _ledger(consumer, settled["builder_id"]) == {
        "grant": 5_000_000,
        "reversal": 5_000_000,
        "lot_regrant": 5_000_000,
    }


# ---------------------------------------------------------------------------
# Full reversal of a Plus cycle and of a 500-credit top-up: exactly once.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [CYCLE[0], TOPUP[0]])
@pytest.mark.parametrize("consumed", [0, 120_000])
async def test_a_full_reversal_is_applied_once_under_replay_and_concurrency(
    consumer: _Harness, fixture: str, consumed: int
) -> None:
    settled = _fresh_aggregate(_fixture(fixture))
    grant = settled["credit_grant_mc"]
    await _seed_payload(consumer, settled)
    await handle_billing_fact(settled)
    builder = UUID(settled["builder_id"])
    if consumed:
        async with consumer.sessions() as session, session.begin():
            result = await CreditLedger(session).debit(
                builder, "trace:prior", consumed, at=T0, effect_kind="trace"
            )
            assert result.status == "applied"
    reversal = _full_chargeback(settled, 2)
    await _seed_payload(consumer, reversal)
    await asyncio.gather(*(handle_billing_fact(reversal) for _ in range(4)))
    await handle_billing_fact(reversal)
    ledger = await _ledger(consumer, settled["builder_id"])
    assert ledger.get("reversal", 0) + ledger.get("debt", 0) == grant
    assert ledger.get("debt", 0) == consumed
    assert (
        await _one(
            consumer,
            "SELECT count(*) FROM credit_effects WHERE effect_kind = 'reversal'",
        )
        == 1
    )
    async with consumer.sessions() as session:
        book = await CreditLedger(session).open_book(builder, at=T0)
        assert book.available_mc(T0) == 0
        assert book.open_debt_mc == consumed
        await session.rollback()


# ---------------------------------------------------------------------------
# Budget and read-only detection.
# ---------------------------------------------------------------------------


async def test_a_call_ends_within_its_budget_and_a_later_one_applies(
    consumer: _Harness,
) -> None:
    payload = _fixture(TOPUP[0])
    await _seed_payload(consumer, payload)
    builder = payload["builder_id"]
    await _exec(
        consumer,
        "INSERT INTO credit_accounts (id, builder_id) VALUES (:id, :b)",
        id=uuid4(),
        b=builder,
    )
    async with consumer.engine.connect() as holder:
        transaction = await holder.begin()
        await holder.execute(
            text("SELECT id FROM credit_accounts WHERE builder_id = :b FOR UPDATE"),
            {"b": builder},
        )
        with pytest.raises(TimeoutError):
            await CreditFactConsumer(
                consumer.sessions, timeout_seconds=0.5, now=lambda: T0
            ).handle(payload)
        await transaction.rollback()
    assert await _one(consumer, "SELECT count(*) FROM credit_fact_inbox") == 0
    # The cancelled connection was invalidated, not left mid-transaction.
    lingering = -1
    for _ in range(50):
        lingering = await _one(
            consumer,
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND state IN "
            "('idle in transaction', 'idle in transaction (aborted)', 'active')",
        )
        if lingering == 0:
            break
        await asyncio.sleep(0.1)
    assert lingering == 0
    await handle_billing_fact(payload)
    assert await _one(consumer, "SELECT count(*) FROM billing_fact_acks") == 1


async def test_reconciliation_detects_by_reading_only(consumer: _Harness) -> None:
    payloads = [_fixture(name) for name in TOPUP]
    for payload in payloads:
        await _seed_payload(consumer, payload)
    await _exec(
        consumer,
        "UPDATE billing_topup_orders SET state = 'quarantined' WHERE id = :id",
        id=payloads[0]["aggregate_id"],
    )
    counts = """
        SELECT (SELECT count(*) FROM credit_fact_inbox),
               (SELECT count(*) FROM credit_effects),
               (SELECT count(*) FROM billing_fact_acks)
    """
    before = await _rows(consumer, counts)
    asaas_before = await _asaas_state(consumer)
    async with consumer.sessions() as session:
        report = await CreditReconciliation(session).report()
    assert (report.facts_without_ack, report.quarantined_lines) == (3, 1)
    assert not report.ready
    assert await _rows(consumer, counts) == before
    assert await _asaas_state(consumer) == asaas_before
    for payload in payloads:
        await handle_billing_fact(payload)
    async with consumer.sessions() as session:
        report = await CreditReconciliation(session).report()
    assert (report.facts_without_ack, report.quarantined_lines) == (0, 1)
    assert not report.ready  # quarantine is accepted as permanent until repaired


# ---------------------------------------------------------------------------
# Review regressions: clocks, reloads and concurrency of one builder.
# ---------------------------------------------------------------------------

_PACKAGE_CENTS = {500: 10_000, 1_000: 20_000, 2_000: 40_000, 4_000: 80_000}


def _topup_fact(builder: str, *, credits: int, paid_at: str) -> dict[str, Any]:
    """A settled top-up fact of one catalog package for ``builder``."""

    fact = _fresh_aggregate(_fixture(TOPUP[0]), builder_id=builder)
    cents, offer = _PACKAGE_CENTS[credits], f"package_credits_{credits}_v1"
    fact.update(
        offer_id=offer,
        credit_grant_mc=credits * 1_000,
        total_amount_cents=cents,
        settled_cents=cents,
        paid_at=paid_at,
    )
    fact["lines"][0].update(
        offer_id=offer,
        credit_grant_mc=credits * 1_000,
        line_amount_cents=cents,
        unit_amount_cents=cents,
        settled_cents=cents,
    )
    return fact


def _snapshot(
    settled: dict[str, Any], revision: int, reversed_cents: int, restored: int
) -> dict[str, Any]:
    fact = _variant(
        settled,
        event_id=str(uuid4()),
        financial_revision=revision,
        event_type=(
            "topup.payment_restored.v1" if restored else "topup.payment_reversed.v1"
        ),
        reversed_cents=reversed_cents,
        restored_cents=restored,
    )
    fact["lines"][0].update(
        reversed_cents=reversed_cents,
        restored_cents=restored,
        financial_state="restored" if restored else "reversed",
    )
    return fact


async def _present(harness: _Harness, fact: dict[str, Any], now: datetime) -> str:
    await _seed_payload(harness, fact)
    _acting_at(now)
    handled = await CreditFactConsumer(harness.sessions, now=lambda: now).handle(
        copy.deepcopy(fact)
    )
    return handled.outcome


async def _position(harness: _Harness, builder: str, at: datetime) -> int:
    _acting_at(at)
    async with harness.sessions() as session, session.begin():
        book = await CreditLedger(session).open_book(UUID(builder), at=at)
        for lot in book.lots:
            if lot.expired(book.clock or at):
                assert lot.balance_mc == 0, lot
        position = book.available_mc(at) - book.open_debt_mc
        await session.rollback()
    return position


async def test_a_writer_with_a_lagging_clock_cannot_wedge_the_builder(
    consumer: _Harness,
) -> None:
    builder = str(uuid4())
    line = _topup_fact(builder, credits=500, paid_at="2026-10-01")
    assert await _present(consumer, line, T0) == "accepted"
    async with consumer.sessions() as session, session.begin():
        debit = await CreditLedger(session).debit(
            UUID(builder), "trace:all", 500_000, at=T0, effect_kind="trace"
        )
        assert debit.debited_mc == 500_000
    reversed_ = _full_chargeback(line, 2)
    assert await _present(consumer, reversed_, T0) == "accepted"
    # Q (paid 2026-04-26, expires 2026-10-26T03:00Z) pays the 500k debt.
    q = _topup_fact(builder, credits=1_000, paid_at="2026-04-26")
    assert await _present(consumer, q, T0) == "accepted"
    deadline = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    # Writer B's clock is past Q's deadline: it records Q's expiry.
    other = _topup_fact(builder, credits=500, paid_at="2026-10-25")
    assert await _present(consumer, other, deadline + timedelta(seconds=2)) == (
        "accepted"
    )
    # Writer A's clock is behind B's: it must not refill the expired Q.
    won = _won(reversed_, 3)
    assert await _present(consumer, won, deadline - timedelta(seconds=2)) == (
        "accepted"
    )
    rows = await _rows(
        consumer,
        "SELECT e.entry_type, e.amount_mc, e.occurred_at FROM credit_ledger_entries e "
        "JOIN credit_lots l ON l.id = e.lot_id WHERE l.order_id = :q "
        "AND (e.effect_id LIKE 'restoration:%' OR e.effect_id LIKE 'return:%') "
        "ORDER BY e.entry_type",
        q=q["aggregate_id"],
    )
    assert [(kind, amount) for kind, amount, _at in rows] == [
        ("expiry", 500_000),
        ("settlement_release", 500_000),
    ]
    assert {at for _kind, _amount, at in rows} == {deadline + timedelta(seconds=2)}
    # Every later fact of the builder still applies.
    later = _topup_fact(builder, credits=500, paid_at="2026-10-26")
    assert await _present(consumer, later, deadline + timedelta(minutes=5)) == (
        "accepted"
    )
    # 500 + 1,000 + 500 + 500 granted, 500 consumed, the chargeback won,
    # Q's unused 1,000 expired (500 at its deadline, 500 returned to it after).
    assert await _position(consumer, builder, deadline + timedelta(minutes=5)) == (
        1_000_000
    )


async def test_the_largest_applied_reversal_target_survives_each_reload(
    consumer: _Harness,
) -> None:
    builder = str(uuid4())
    line = _topup_fact(builder, credits=2_000, paid_at="2026-10-01")  # 40,000 cents
    assert await _present(consumer, line, T0) == "accepted"
    for fact in (
        _snapshot(line, 2, 12_000, 0),  # 30% reversed
        _snapshot(line, 3, 24_000, 0),  # 60% reversed
        _snapshot(line, 4, 24_000, 4_000),  # 60% reversed, 10% restored
    ):
        assert await _present(consumer, fact, T0) == "accepted"  # own transaction
    assert await _ledger(consumer, builder) == {
        "grant": 2_000_000,
        "reversal": 1_200_000,
        "lot_regrant": 200_000,
    }
    assert await _position(consumer, builder, T0) == 1_000_000


async def test_growing_partial_reversals_then_a_won_chargeback_after_reloads(
    consumer: _Harness,
) -> None:
    builder = str(uuid4())
    line = _topup_fact(builder, credits=2_000, paid_at="2026-10-01")
    other = _topup_fact(builder, credits=4_000, paid_at="2026-10-02")
    for fact in (line, other):
        assert await _present(consumer, fact, T0) == "accepted"
    async with consumer.sessions() as session, session.begin():
        await CreditLedger(session).debit(
            UUID(builder), "trace:p", 1_800_000, at=T0, effect_kind="trace"
        )
    for fact in (
        _snapshot(line, 2, 12_000, 0),
        _snapshot(line, 3, 24_000, 0),
        _snapshot(line, 4, 24_000, 24_000),
    ):
        assert await _present(consumer, fact, T0) == "accepted"
    # 6,000 granted, 1,800 consumed, the reversal fully won: 4,200 credits.
    assert await _position(consumer, builder, T0) == 4_200_000


async def test_concurrent_facts_and_debits_of_one_builder_serialize(
    consumer: _Harness,
) -> None:
    builder = str(uuid4())
    a = _topup_fact(builder, credits=2_000, paid_at="2026-10-01")
    b = _topup_fact(builder, credits=1_000, paid_at="2026-09-01")
    a_reversed = _snapshot(a, 2, 20_000, 0)
    a_won = _snapshot(a, 3, 20_000, 10_000)
    facts = [a, b, a_reversed, a_won]
    for fact in facts:
        await _seed_payload(consumer, fact)

    async def present(fact: dict[str, Any]) -> str:
        consumer_ = CreditFactConsumer(consumer.sessions, now=lambda: T0)
        return (await consumer_.handle(copy.deepcopy(fact))).outcome

    async def debit(number: int) -> str:
        async with consumer.sessions() as session, session.begin():
            result = await CreditLedger(session).debit(
                UUID(builder), f"trace:c{number}", 150_000, at=T0, effect_kind="trace"
            )
        return result.status

    results = await asyncio.gather(
        *(present(fact) for fact in facts for _ in range(4)),
        *(debit(number) for number in range(12)),
    )
    assert set(results) <= {"accepted", "replay", "stale", "applied"}
    assert results.count("applied") == 12
    counts = await _rows(
        consumer,
        "SELECT (SELECT count(*) FROM billing_fact_acks a JOIN billing_outbox o "
        "ON o.id = a.outbox_event_id WHERE o.builder_id = :b), "
        "(SELECT count(*) FROM credit_lots WHERE builder_id = :b), "
        "(SELECT count(*) FROM credit_effects WHERE builder_id = :b "
        "AND effect_kind = 'trace')",
        b=builder,
    )
    assert tuple(counts[0]) == (4, 2, 12)
    # 3,000 granted; A reversed 2,000 then restored 1,000; 12 x 150 consumed.
    assert await _position(consumer, builder, T0) == 700_000


async def test_a_cycle_whose_restored_snapshot_arrives_first(
    consumer: _Harness,
) -> None:
    settled, reversed_, restored = (_fixture(name) for name in CYCLE)
    outcomes = [
        await _present(consumer, fact, T0) for fact in (restored, settled, reversed_)
    ]
    assert outcomes == ["accepted", "stale", "stale"]
    assert await _rows(
        consumer, "SELECT status, financial_revision FROM credit_enforcement_binding"
    ) == [("active", 3)]
    gaps = await _rows(
        consumer, "SELECT closed_at IS NOT NULL FROM credit_revision_gaps"
    )
    assert gaps and all(closed for (closed,) in gaps)


# ---------------------------------------------------------------------------
# Review round 2: clocks ahead of the database, the release cascade.
# ---------------------------------------------------------------------------


def _reference(instant: datetime) -> Any:
    async def clock() -> datetime:
        return instant

    return clock


async def _book_at(harness: _Harness, builder: str, at: datetime) -> Any:
    """The builder's book at a simulated instant (reference clock = it)."""

    async with harness.sessions() as session, session.begin():
        ledger = CreditLedger(session, reference_clock=_reference(at))
        book = await ledger.open_book(UUID(builder), at=at)
        await session.rollback()
    return book


async def _acks_of(harness: _Harness, fact: dict[str, Any]) -> int:
    return int(
        await _one(
            harness,
            "SELECT count(*) FROM billing_fact_acks WHERE event_id = :e",
            e=fact["event_id"],
        )
    )


class _Warnings:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


async def test_a_writer_ahead_of_the_database_clock_acts_at_the_tolerance(
    consumer: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner decision C on the real ``clock_timestamp()``: an application
    clock 10 minutes ahead applies the fact at database clock + 5 minutes,
    with a warning; the builder keeps working afterwards."""

    from src.services import credit_ledger as ledger_module

    warnings = _Warnings()
    monkeypatch.setattr(ledger_module, "logger", warnings)
    _acting_at(None)  # the real clock_timestamp() of this PostgreSQL
    builder = str(uuid4())
    fact = _topup_fact(builder, credits=500, paid_at="2026-10-01")
    await _seed_payload(consumer, fact)
    before = await _one(consumer, "SELECT clock_timestamp()")
    handled = await CreditFactConsumer(
        consumer.sessions, now=lambda: before + timedelta(minutes=10)
    ).handle(copy.deepcopy(fact))
    after = await _one(consumer, "SELECT clock_timestamp()")
    assert handled.outcome == "accepted"
    tolerance = timedelta(minutes=5)
    stamps = await _rows(
        consumer,
        "SELECT occurred_at FROM credit_ledger_entries WHERE builder_id = :b "
        "UNION ALL SELECT received_at FROM credit_fact_inbox WHERE event_id = :e "
        "UNION ALL SELECT acknowledged_at FROM billing_fact_acks WHERE event_id = :e",
        b=builder,
        e=fact["event_id"],
    )
    assert len(stamps) == 3
    for (stamp,) in stamps:
        assert before + tolerance <= stamp <= after + tolerance
    (_event, fields), *_ = warnings.events
    assert fields["tolerance_seconds"] == 300
    assert 290 <= fields["seconds_ahead"] <= 600
    assert await _acks_of(consumer, fact) == 1
    # A Sao Paulo wall clock read as UTC (3 hours ahead): limited the same way.
    real = datetime.now(timezone.utc)
    utc_wall_read_as_sao_paulo = real.replace(tzinfo=None).replace(
        tzinfo=ZoneInfo("America/Sao_Paulo")
    )
    other = _topup_fact(builder, credits=500, paid_at="2026-10-02")
    await _seed_payload(consumer, other)
    assert (
        await CreditFactConsumer(
            consumer.sessions, now=lambda: utc_wall_read_as_sao_paulo
        ).handle(copy.deepcopy(other))
    ).outcome == "accepted"
    assert "expiry" not in await _ledger(consumer, builder)


async def test_a_ledger_clock_already_ahead_fails_closed(consumer: _Harness) -> None:
    builder = str(uuid4())
    poisoned = datetime.now(timezone.utc) + timedelta(days=200)
    async with consumer.sessions() as session, session.begin():
        # An older writer without the guard (simulated by a reference clock).
        ledger = CreditLedger(session, reference_clock=_reference(poisoned))
        book = await ledger.open_book(UUID(builder), at=poisoned)
        book.debit("trace:poison", 1, at=poisoned, effect_kind="trace")
        await ledger.persist(book)
    later = _topup_fact(builder, credits=500, paid_at="2026-10-20")
    with pytest.raises(LedgerClockAhead, match="ledger clock"):
        await _present(consumer, later, T0)
    assert await _acks_of(consumer, later) == 0


async def test_a_won_chargeback_returns_capacity_to_the_lot_that_paid(
    consumer: _Harness,
) -> None:
    builder = str(uuid4())
    a = _topup_fact(builder, credits=1_000, paid_at="2026-10-01")
    assert await _present(consumer, a, T0) == "accepted"
    async with consumer.sessions() as session, session.begin():
        await CreditLedger(session).debit(
            UUID(builder), "trace:all", 1_000_000, at=T0, effect_kind="trace"
        )
    a_back = _full_chargeback(a, 2)
    b = _topup_fact(builder, credits=1_000, paid_at="2026-04-27")  # ends 10-27
    b_back = _full_chargeback(b, 2)
    c = _topup_fact(builder, credits=1_000, paid_at="2026-10-03")
    for fact in (a_back, b, b_back, c, _won(a_back, 3)):
        assert await _present(consumer, fact, T0) == "accepted"
    book = await _book_at(consumer, builder, T0)
    by_order = {str(lot.order_id): lot for lot in book.lots}
    assert by_order[b["aggregate_id"]].balance_mc == 0
    assert by_order[c["aggregate_id"]].balance_mc == 1_000_000
    assert book.open_debt_mc == 0
    for lot in book.lots:
        assert book.reversal_debt_net_mc(lot.id) <= lot.consumed_mc
    after_b = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
    book = await _book_at(consumer, builder, after_b)
    assert book.available_mc(after_b) - book.open_debt_mc == 1_000_000  # D1


async def test_a_cascade_into_the_monthly_allowance_expires_with_it(
    consumer: _Harness,
) -> None:
    cycle = _fixture(CYCLE[0])  # Plus, 2026-10-12 .. 2026-11-12
    builder = cycle["builder_id"]
    a = _topup_fact(builder, credits=1_000, paid_at="2026-10-01")
    assert await _present(consumer, a, T0) == "accepted"
    async with consumer.sessions() as session, session.begin():
        await CreditLedger(session).debit(
            UUID(builder), "trace:all", 1_000_000, at=T0, effect_kind="trace"
        )
    a_back = _full_chargeback(a, 2)
    b = _topup_fact(builder, credits=1_000, paid_at="2026-10-02")
    for fact in (a_back, b, _full_chargeback(b, 2), cycle, _won(a_back, 3)):
        assert await _present(consumer, fact, T0) == "accepted"
    book = await _book_at(consumer, builder, T0)
    monthly = next(lot for lot in book.lots if lot.lot_kind == "monthly")
    assert monthly.balance_mc == 5_000_000  # its 1,000 came back, not to B
    assert book.available_mc(T0) == 5_000_000 and book.open_debt_mc == 0
    after_cycle = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    book = await _book_at(consumer, builder, after_cycle)
    assert book.available_mc(after_cycle) - book.open_debt_mc == 0  # D2


async def test_crossed_clocks_and_concurrency_never_wedge(consumer: _Harness) -> None:
    rng = random.Random(20261026)
    deadline = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    for round_ in range(4):
        builder = str(uuid4())
        a = _topup_fact(builder, credits=500, paid_at="2026-10-01")
        day_before = deadline - timedelta(days=1)
        assert await _present(consumer, a, day_before) == "accepted"
        async with consumer.sessions() as session, session.begin():
            await CreditLedger(session).debit(
                UUID(builder), "trace:all", 500_000, at=day_before, effect_kind="trace"
            )
        a_back = _full_chargeback(a, 2)
        assert await _present(consumer, a_back, day_before) == "accepted"
        q = _topup_fact(
            builder, credits=1_000, paid_at="2026-04-26"
        )  # ends at deadline
        assert await _present(consumer, q, day_before) == "accepted"
        facts = [_won(a_back, 3)] + [
            _topup_fact(builder, credits=500, paid_at="2026-10-25") for _ in range(3)
        ]
        for fact in facts:
            await _seed_payload(consumer, fact)
        _acting_at(deadline)

        def clock() -> datetime:
            return deadline + timedelta(seconds=rng.randint(-5, 5))

        async def present(fact: dict[str, Any]) -> str:
            consumer_ = CreditFactConsumer(consumer.sessions, now=clock)
            return (await consumer_.handle(copy.deepcopy(fact))).outcome

        async def debit(number: int) -> str:
            async with consumer.sessions() as session, session.begin():
                result = await CreditLedger(session).debit(
                    UUID(builder),
                    f"trace:r{round_}:{number}",
                    120_000,
                    at=clock(),
                    effect_kind="trace",
                )
            return result.status

        results = await asyncio.gather(
            *(present(fact) for fact in facts for _ in range(2)),
            *(debit(number) for number in range(6)),
        )
        assert set(results) <= {"accepted", "replay", "applied"}
        later = _topup_fact(builder, credits=500, paid_at="2026-10-26")
        assert await _present(consumer, later, deadline + timedelta(minutes=5)) == (
            "accepted"
        )
        book = await _book_at(consumer, builder, deadline + timedelta(minutes=5))
        for lot in book.lots:
            if lot.expired(deadline + timedelta(minutes=5)):
                assert lot.balance_mc == 0
            assert book.reversal_debt_net_mc(lot.id) <= lot.consumed_mc


# ---------------------------------------------------------------------------
# Review round 3: the payer's own reversal debt, reloads, the database clock.
# ---------------------------------------------------------------------------


async def _lot_balances(harness: _Harness, builder: str) -> list[int]:
    """Each lot's balance, by payment date."""

    rows = await _rows(
        harness,
        "SELECT sum(CASE WHEN e.entry_type IN ('grant', 'settlement_release', "
        "'lot_regrant') THEN e.amount_mc ELSE -e.amount_mc END) "
        "FROM credit_lots l JOIN credit_ledger_entries e ON e.lot_id = l.id "
        "WHERE l.builder_id = :b GROUP BY l.id, l.paid_at ORDER BY l.paid_at",
        b=builder,
    )
    return [int(balance) for (balance,) in rows]


async def _c8(harness: _Harness, with_chargeback: bool) -> tuple[list[int], int]:
    builder = str(uuid4())
    a = _topup_fact(builder, credits=1_000, paid_at="2026-04-25")  # ends 10-25
    assert await _present(harness, a, T0) == "accepted"
    async with harness.sessions() as session, session.begin():
        await CreditLedger(session).debit(
            UUID(builder), "trace:a", 1_000_000, at=T0, effect_kind="trace"
        )
    a_back = _full_chargeback(a, 2)
    if with_chargeback:
        assert await _present(harness, a_back, T0) == "accepted"
    p = _topup_fact(builder, credits=2_000, paid_at="2026-04-30")  # ends 10-30
    assert await _present(harness, p, T0) == "accepted"  # pays A's debt
    async with harness.sessions() as session, session.begin():
        await CreditLedger(session).debit(
            UUID(builder), "trace:p", 1_000_000, at=T0, effect_kind="trace"
        )
    half = _snapshot(p, 2, p["settled_cents"] // 2, 0)  # P half refunded
    assert await _present(harness, half, T0) == "accepted"
    q = _topup_fact(builder, credits=1_000, paid_at="2026-10-20")
    assert await _present(harness, q, T0) == "accepted"
    if with_chargeback:
        assert await _present(harness, _won(a_back, 3), T0) == "accepted"
    later = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)  # after P's end
    book = await _book_at(harness, builder, later)
    for lot in book.lots:
        if lot.available_mc(T0) > 0:
            assert book.reversal_debt_net_mc(lot.id) == 0
    return await _lot_balances(harness, builder), (
        book.available_mc(later) - book.open_debt_mc
    )


async def test_a_won_chargeback_never_leaves_capacity_on_a_charged_back_payer(
    consumer: _Harness,
) -> None:
    """The reviewer's C8: P paid A's debt, then was half refunded; Q pays
    P's debt.  A's won chargeback must cost what no chargeback costs."""

    with_chargeback = await _c8(consumer, True)
    without = await _c8(consumer, False)
    assert with_chargeback == without == ([0, 0, 1_000_000], 1_000_000)


async def test_a_restoration_split_over_two_facts_attributes_its_releases(
    consumer: _Harness,
) -> None:
    """The reviewer's m01 through facts: each fact reloads the book; the
    second restoration must see what the first released to Q."""

    builder = str(uuid4())
    line = _topup_fact(builder, credits=1_000, paid_at="2026-10-01")
    assert await _present(consumer, line, T0) == "accepted"
    async with consumer.sessions() as session, session.begin():
        await CreditLedger(session).debit(
            UUID(builder), "trace:1", 600_000, at=T0, effect_kind="trace"
        )
    assert await _present(consumer, _full_chargeback(line, 2), T0) == "accepted"
    q = _topup_fact(builder, credits=1_000, paid_at="2026-10-02")  # pays 600
    assert await _present(consumer, q, T0) == "accepted"
    cents = line["settled_cents"]
    assert await _present(consumer, _snapshot(line, 3, cents, cents // 2), T0) == (
        "accepted"
    )
    assert await _present(consumer, _snapshot(line, 4, cents, cents), T0) == (
        "accepted"
    )
    assert await _lot_balances(consumer, builder) == [400_000, 1_000_000]
    book = await _book_at(consumer, builder, T0)
    assert book.open_debt_mc == 0


async def test_the_reference_is_postgresql_s_clock_timestamp(
    consumer: _Harness,
) -> None:
    _acting_at(None)  # the real database clock
    statements: list[str] = []

    def capture(_c: Any, _cursor: Any, statement: str, *_: Any) -> None:
        statements.append(statement)

    builder = uuid4()
    real = await _one(consumer, "SELECT clock_timestamp()")
    event.listen(consumer.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with consumer.sessions() as session, session.begin():
            ledger = CreditLedger(session)
            book = await ledger.open_book(builder, at=real - timedelta(days=1))
            result = book.debit(
                "trace:1", 1, at=real - timedelta(days=1), effect_kind="t"
            )
            await ledger.persist(book)
    finally:
        event.remove(consumer.engine.sync_engine, "before_cursor_execute", capture)
    assert any("clock_timestamp()" in statement for statement in statements)
    after = await _one(consumer, "SELECT clock_timestamp()")
    # A writer a day behind is raised to the database clock - 5 minutes.
    tolerance = timedelta(minutes=5)
    assert real - tolerance <= result.at <= after - tolerance
    stored = await _one(
        consumer,
        "SELECT occurred_at FROM credit_ledger_entries WHERE effect_id = 'trace:1' "
        "AND builder_id = :b",
        b=builder,
    )
    assert stored == result.at
    # And one ahead of it is limited to the database clock + 5 minutes.
    async with consumer.sessions() as session, session.begin():
        book = await CreditLedger(session).open_book(
            uuid4(), at=after + timedelta(days=1)
        )
    final = await _one(consumer, "SELECT clock_timestamp()")
    assert after + tolerance <= book.clock <= final + tolerance


async def _bookkeeping(harness: _Harness, builder: str, event_id: str) -> tuple:
    (row,) = await _rows(
        harness,
        "SELECT (SELECT count(*) FROM credit_accounts WHERE builder_id = :b), "
        "(SELECT count(*) FROM credit_effects WHERE builder_id = :b), "
        "(SELECT count(*) FROM credit_fact_inbox WHERE event_id = :e), "
        "(SELECT count(*) FROM billing_fact_acks WHERE event_id = :e), "
        "(SELECT count(*) FROM credit_revision_gaps WHERE closed_at IS NULL)",
        b=builder,
        e=event_id,
    )
    return tuple(row)


async def test_a_clock_ahead_is_limited_on_every_path(consumer: _Harness) -> None:
    """Owner decision C: applied, rejected and older-revision facts presented
    with an application clock 200 days ahead are all written, dated at the
    database clock + 5 minutes; a replay stays a replay."""

    ahead = T0 + timedelta(days=200)
    limit = T0 + timedelta(minutes=5)  # T0 is this suite's database clock

    async def early(fact: dict[str, Any]) -> str:
        handled = await CreditFactConsumer(consumer.sessions, now=lambda: ahead).handle(
            copy.deepcopy(fact)
        )
        return handled.outcome

    builder = str(uuid4())
    settled = _topup_fact(builder, credits=500, paid_at="2026-10-01")
    await _seed_payload(consumer, settled)
    _acting_at(T0)
    assert await early(settled) == "accepted"
    other = _topup_fact(builder, credits=500, paid_at="2026-10-02")
    await _seed_payload(consumer, other)
    tampered = copy.deepcopy(other)
    tampered["credit_grant_mc"] = tampered["lines"][0]["credit_grant_mc"] = 1
    assert await early(tampered) == "rejected"
    cents = settled["settled_cents"]
    newer = _snapshot(settled, 3, cents, 0)
    await _seed_payload(consumer, newer)
    assert await early(newer) == "accepted"  # opens the gap at revision 2
    old = _snapshot(settled, 2, cents // 2, 0)
    await _seed_payload(consumer, old)
    assert await early(old) == "stale"  # acked, closes the gap
    assert await early(settled) == "replay"
    for fact in (settled, tampered, newer, old):
        assert await _acks_of(consumer, fact) == 1
    events = [UUID(fact["event_id"]) for fact in (settled, tampered, newer, old)]
    stamps = await _rows(
        consumer,
        "SELECT occurred_at FROM credit_ledger_entries WHERE builder_id = :b "
        "UNION SELECT received_at FROM credit_fact_inbox "
        "WHERE event_id = ANY(:events) "
        "UNION SELECT acknowledged_at FROM billing_fact_acks "
        "WHERE event_id = ANY(:events) "
        "UNION SELECT opened_at FROM credit_revision_gaps WHERE aggregate_id = :g "
        "UNION SELECT closed_at FROM credit_revision_gaps WHERE aggregate_id = :g",
        b=builder,
        events=events,
        g=settled["aggregate_id"],
    )
    assert {stamp for (stamp,) in stamps} == {limit}


async def test_a_ledger_clock_beyond_the_tolerance_refuses_writing_nothing(
    consumer: _Harness,
) -> None:
    """The one refusal left: a builder whose ledger clock is already beyond
    database clock + tolerance (the database clock stepped back)."""

    builder = str(uuid4())
    first = _topup_fact(builder, credits=500, paid_at="2026-10-01")
    assert await _present(consumer, first, T0 + timedelta(minutes=4)) == "accepted"
    _acting_at(T0 - timedelta(minutes=2))  # the database clock steps back
    later = _topup_fact(builder, credits=500, paid_at="2026-10-02")
    await _seed_payload(consumer, later)
    before = await _bookkeeping(consumer, builder, later["event_id"])
    with pytest.raises(LedgerClockAhead, match="ledger clock"):
        await CreditFactConsumer(
            consumer.sessions, now=lambda: T0 - timedelta(minutes=2)
        ).handle(copy.deepcopy(later))
    assert await _bookkeeping(consumer, builder, later["event_id"]) == before
    assert await _acks_of(consumer, later) == 0  # presented again later
    _acting_at(T0 - timedelta(seconds=59))  # caught up
    assert (
        await CreditFactConsumer(
            consumer.sessions, now=lambda: T0 - timedelta(seconds=59)
        ).handle(copy.deepcopy(later))
    ).outcome == "accepted"


async def _won_at_once_history(harness: _Harness, with_pair: bool) -> str:
    """The reviewer's R4-1b through facts: X 50% refunded, its debt paid by
    top-up P1 (which then expires); the allowance pays a further 25%, won at
    once."""

    t1 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    builder = str(uuid4())
    x = _topup_fact(builder, credits=1_000, paid_at="2026-10-01")
    assert await _present(harness, x, T0) == "accepted"
    async with harness.sessions() as session, session.begin():
        await CreditLedger(session).debit(
            UUID(builder), "trace:all", 1_000_000, at=T0, effect_kind="trace"
        )
    p1 = _topup_fact(builder, credits=1_000, paid_at="2026-04-25")  # ends 10-25
    assert await _present(harness, p1, T0) == "accepted"
    cents = x["settled_cents"]
    assert await _present(harness, _snapshot(x, 2, cents // 2, 0), T0) == "accepted"
    month = _fresh_aggregate(_fixture(CYCLE[0]), builder_id=builder)
    assert await _present(harness, month, t1) == "accepted"  # P1 expired by now
    if with_pair:
        three_quarters = cents * 3 // 4
        assert await _present(harness, _snapshot(x, 3, three_quarters, 0), t1) == (
            "accepted"
        )
        assert await _present(
            harness, _snapshot(x, 4, three_quarters, cents // 4), t1
        ) == ("accepted")
    return builder


async def test_a_reversal_won_at_once_never_costs_the_customer(
    consumer: _Harness,
) -> None:
    t1 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    with_pair = await _won_at_once_history(consumer, True)
    without = await _won_at_once_history(consumer, False)
    assert await _position(consumer, with_pair, t1) == await _position(
        consumer, without, t1
    )
    assert await _lot_balances(consumer, with_pair) == await _lot_balances(
        consumer, without
    )
    assert await _position(consumer, without, t1) == 5_000_000
