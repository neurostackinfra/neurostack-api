"""Credit consumer of ``billing.fact.v1`` (neurostack-credit-system Group 2).

SQLite proofs of the consumer's decisions.  SQLite stores a ``UUID`` column
with NUMERIC affinity, so the fixtures' all-digit ids are remapped to
non-numeric ones here (payload digests recomputed, as the producer would
store them); the exact fixture bytes are replayed on PostgreSQL in
``tests/integration/test_credit_fact_replay_postgres.py``.  SQLite cannot
generate ``inbox_seq`` (a PostgreSQL identity), so these tests assign it.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import itertools
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.models.base import Base
from src.models.billing import (
    BillingFactAck,
    BillingFactDelivery,
    BillingFactHandoff,
    BillingOutboxEvent,
)
from src.models.credit import (
    CreditEffect,
    CreditEnforcementBinding,
    CreditLedgerEntry,
    CreditLot,
)
from src.models.credit_fact_inbox import CreditFactInbox, CreditRevisionGap
from src.repositories.credit import FactOutboxRow
from src.repositories.credit_fact_ack import (
    EMPTY_EFFECTS_DIGEST,
    FactHandoff,
    effects_digest,
)
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import billing_outbox as outbox_module
from src.services import credit_fact_consumer as consumer_module
from src.services.billing_catalog import BILLING_CATALOG, catalog_snapshot_bytes
from src.services.billing_fact_worker import (
    TRANSPORT_TIMEOUT_SECONDS,
    BillingFactWorker,
    BillingFactWorkerConfig,
)
from src.services.billing_outbox import (
    BillingFactViolation,
    PresentationPreconditionsOpen,
    billing_fact_bundle_digest,
    billing_fact_schema_digest,
    payload_digest,
)
from src.services.credit_fact_consumer import (
    CONSUMED_CATALOG_VERSION_ID,
    CONSUMED_OFFERS,
    PINNED_BUNDLE_DIGEST,
    PINNED_CATALOG_SNAPSHOT_SHA256,
    PINNED_SCHEMA_DIGEST,
    ConsumerContractDivergence,
    CreditFactConsumer,
    CreditSwitchOff,
    CycleIdentityConflict,
    FactCollision,
    UnresolvableFact,
    binding_status,
    check_fact,
    consumer_contract_digests,
    handle_billing_fact,
)
from src.services.credit_switch import CreditSwitch

_ROOT = Path(__file__).parents[2]
FIXTURES = _ROOT / "tests/fixtures/contracts/billing_fact_v1"
CYCLE = (
    "subscription_cycle_settled",
    "subscription_cycle_reversed",
    "subscription_cycle_restored",
)
TOPUP = ("topup_payment_settled", "topup_payment_reversed", "topup_payment_restored")
DEPLOYMENT = hashlib.sha256(b"billing.fact.v1:deployment-unverified").hexdigest()
T0 = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
_ID_FIELDS = ("event_id", "builder_id", "aggregate_id", "subscription_id", "order_id")


def _fixture(name: str) -> dict[str, Any]:
    return dict(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


def _sqlite_safe(value: str) -> str:
    """``d0000000-...`` -> ``<letter>0000000-...``: same identity, not numeric."""

    return "abcdefghij"[int(value[0])] + value[1:] if value[0].isdigit() else value


def _remapped(name: str) -> dict[str, Any]:
    payload = _fixture(name)
    for field_name in _ID_FIELDS:
        if isinstance(payload.get(field_name), str):
            payload[field_name] = _sqlite_safe(payload[field_name])
    return payload


def _fresh(payload: dict[str, Any], **ids: UUID) -> dict[str, Any]:
    """A copy with new ids (``event_id`` always new)."""

    copied = copy.deepcopy(payload)
    copied["event_id"] = str(ids.pop("event_id", uuid4()))
    for field_name, value in ids.items():
        copied[field_name] = str(value)
    if copied["aggregate_kind"] == "topup_order" and "aggregate_id" in ids:
        copied["order_id"] = str(ids["aggregate_id"])
    return copied


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    finally:
        await engine.dispose()


@pytest.fixture
def inbox_seq() -> Iterator[None]:
    """SQLite stand-in for the PostgreSQL ``inbox_seq`` identity."""

    counter = itertools.count(1)

    def assign(_mapper: Any, _connection: Any, target: CreditFactInbox) -> None:
        if target.inbox_seq is None:
            target.inbox_seq = next(counter)

    event.listen(CreditFactInbox, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(CreditFactInbox, "before_insert", assign)


@pytest.fixture
def consumer_on(monkeypatch: pytest.MonkeyPatch, inbox_seq: None) -> None:
    """Test-only waiver of the producer's presentation guard + switch on."""

    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", False)
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)


async def _seed(
    sessions: async_sessionmaker[AsyncSession],
    payload: dict[str, Any],
    *,
    stored_digest: str | None = None,
    aggregate_id: UUID | None = None,
) -> UUID:
    """The producer rows a presented fact names (outbox, delivery, handoff)."""

    kind = payload["aggregate_kind"]
    aggregate = aggregate_id or UUID(payload["aggregate_id"])
    outbox_id = UUID(payload["event_id"])
    async with sessions() as session, session.begin():
        session.add(
            BillingOutboxEvent(
                id=outbox_id,
                builder_id=UUID(payload["builder_id"]),
                schema_version="billing.fact.v1",
                event_type=payload["event_type"],
                aggregate_kind=kind,
                aggregate_id=aggregate,
                subscription_id=(
                    UUID(payload["subscription_id"])
                    if kind == "subscription_cycle"
                    else None
                ),
                topup_order_id=aggregate if kind == "topup_order" else None,
                subscription_cycle_id=(
                    aggregate if kind == "subscription_cycle" else None
                ),
                financial_revision=payload["financial_revision"],
                payload=payload,
                payload_sha256=stored_digest or payload_digest(payload),
                bundle_digest=PINNED_BUNDLE_DIGEST,
                deployment_digest=DEPLOYMENT,
            )
        )
        await session.flush()
        delivery = BillingFactDelivery(
            id=uuid4(), outbox_event_id=outbox_id, event_id=outbox_id, state="pending"
        )
        session.add(delivery)
        await session.flush()
        session.add(
            BillingFactHandoff(
                id=uuid4(),
                outbox_event_id=outbox_id,
                delivery_id=delivery.id,
                schema_version="billing.fact.v1",
                bundle_digest=PINNED_BUNDLE_DIGEST,
                deployment_digest=DEPLOYMENT,
                handed_off_at=T0,
            )
        )
    return outbox_id


async def _count(sessions: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sessions() as session:
        return int(
            (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
        )


def _at(instant: datetime) -> Any:
    """A database clock reading the test's simulated instant."""

    async def clock() -> datetime:
        return instant

    return clock


def _consumer(sessions: async_sessionmaker[AsyncSession]) -> CreditFactConsumer:
    return CreditFactConsumer(sessions, now=lambda: T0, reference_clock=_at(T0))


# ---------------------------------------------------------------------------
# Pins and the consumer-side contract check.
# ---------------------------------------------------------------------------


def test_pins_equal_the_bundle_verifier_and_the_producer_digests() -> None:
    emitted = subprocess.run(
        [
            sys.executable,
            "scripts/verify-billing-fact-bundle.py",
            "--schema",
            "src/contracts/billing_fact_v1.schema.json",
            "--fixtures",
            "tests/fixtures/contracts/billing_fact_v1",
            "--manifest",
            "tests/fixtures/contracts/billing_fact_v1/SHA256SUMS",
            "--emit-digest",
        ],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert emitted == PINNED_BUNDLE_DIGEST == billing_fact_bundle_digest()
    assert billing_fact_schema_digest() == PINNED_SCHEMA_DIGEST
    manifest = (FIXTURES / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"{PINNED_SCHEMA_DIGEST}  src/contracts/billing_fact_v1.schema.json" in (
        manifest.splitlines()
    )
    assert hashlib.sha256(catalog_snapshot_bytes()).hexdigest() == (
        PINNED_CATALOG_SNAPSHOT_SHA256
    )
    digests = consumer_contract_digests()
    assert (digests.bundle_digest, digests.schema_digest) == (
        PINNED_BUNDLE_DIGEST,
        PINNED_SCHEMA_DIGEST,
    )


def test_the_consumed_catalog_is_the_pdf_catalog() -> None:
    assert set(CONSUMED_OFFERS) == set(BILLING_CATALOG)
    for offer_id, offer in BILLING_CATALOG.items():
        consumed = CONSUMED_OFFERS[offer_id]
        assert consumed.credits == offer.credits
        assert (consumed.aggregate_kind == "subscription_cycle") == (
            offer.cycle == "MONTHLY"
        )
    assert [CONSUMED_OFFERS[o].credits for o in sorted(CONSUMED_OFFERS)] == [
        1_000,
        2_000,
        4_000,
        500,
        35_000,
        5_000,
        15_000,
    ]
    assert CONSUMED_CATALOG_VERSION_ID == "payment-2026-10-03"


@pytest.mark.parametrize(
    "patch",
    [
        {"billing_fact_bundle_digest": "raise"},
        {"billing_fact_bundle_digest": "0" * 64},
        {"billing_fact_schema_digest": "0" * 64},
        {"catalog_snapshot_bytes": b"{}"},
    ],
)
def test_a_consumer_side_divergence_fails_closed(
    monkeypatch: pytest.MonkeyPatch, patch: dict[str, object]
) -> None:
    for name, value in patch.items():
        if value == "raise":

            def unavailable() -> str:
                raise BillingFactViolation(
                    "billing fact bundle manifest is unavailable"
                )

            monkeypatch.setattr(consumer_module, name, unavailable)
        else:
            monkeypatch.setattr(consumer_module, name, lambda value=value: value)
    with pytest.raises(ConsumerContractDivergence):
        consumer_contract_digests()


# ---------------------------------------------------------------------------
# Fact checks: a problem in the fact is ``rejected``.
# ---------------------------------------------------------------------------


def _row(payload: dict[str, Any]) -> FactOutboxRow:
    kind = payload["aggregate_kind"]
    aggregate = UUID(payload["aggregate_id"])
    return FactOutboxRow(
        outbox_event_id=UUID(payload["event_id"]),
        builder_id=UUID(payload["builder_id"]),
        schema_version="billing.fact.v1",
        aggregate_kind=kind,
        aggregate_id=aggregate,
        financial_revision=payload["financial_revision"],
        subscription_id=(
            UUID(payload["subscription_id"]) if kind == "subscription_cycle" else None
        ),
        topup_order_id=aggregate if kind == "topup_order" else None,
        subscription_cycle_id=aggregate if kind == "subscription_cycle" else None,
    )


def _handoff(payload: dict[str, Any], stored: str | None = None) -> FactHandoff:
    return FactHandoff(
        outbox_event_id=UUID(payload["event_id"]),
        event_id=UUID(payload["event_id"]),
        payload_sha256=stored or payload_digest(payload),
        bundle_digest=PINNED_BUNDLE_DIGEST,
        deployment_digest=DEPLOYMENT,
    )


def _check(payload: dict[str, Any], **overrides: Any) -> object:
    return check_fact(
        payload,
        outbox=overrides.get("outbox", _row(payload)),
        handoff=overrides.get("handoff", _handoff(payload)),
        fact_sha256=payload_digest(payload),
    )


@pytest.mark.parametrize("name", CYCLE + TOPUP)
def test_the_six_fixtures_pass_every_fact_check(name: str) -> None:
    payload = _fixture(name)
    checked = _check(payload)
    assert not isinstance(checked, str), checked


def _partial_credit_line() -> dict[str, Any]:
    payload = _fixture("topup_payment_settled")
    (line,) = payload["lines"]
    line["settled_cents"] = payload["settled_cents"] = 5_000
    return payload


def _ambiguous_allocation() -> dict[str, Any]:
    payload = _fixture("subscription_cycle_settled")
    clinic = payload["lines"][0]
    clinic["settled_cents"], clinic["financial_state"] = 0, "pending"
    return payload  # envelope still says 119900 settled


def _repriced() -> dict[str, Any]:
    payload = _fixture("topup_payment_settled")
    payload["lines"][0]["credit_grant_mc"] = payload["credit_grant_mc"] = 600_000
    return payload


def _foreign_catalog() -> dict[str, Any]:
    payload = _fixture("topup_payment_settled")
    payload["catalog_version_id"] = "payment-2099-01-01"
    return payload


def _taxed() -> dict[str, Any]:
    payload = _fixture("subscription_cycle_settled")
    payload["lines"][0]["kind"] = "tax"
    return payload


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (_partial_credit_line, "partial_credit_line"),
        (_ambiguous_allocation, "ambiguous_line_allocation"),
        (_repriced, "catalog_mismatch"),
        (_foreign_catalog, "catalog_mismatch"),
        (_taxed, "catalog_mismatch"),
    ],
)
def test_a_problem_in_the_fact_names_its_reason(build: Any, reason: str) -> None:
    assert _check(build()) == reason


def test_a_tampered_payload_or_row_is_rejected() -> None:
    payload = _fixture("topup_payment_settled")
    stored = payload_digest(payload)
    tampered = copy.deepcopy(payload)
    tampered["credit_grant_mc"] = 4_000_000
    assert (
        check_fact(
            tampered,
            outbox=_row(payload),
            handoff=_handoff(payload, stored),
            fact_sha256=payload_digest(tampered),
        )
        == "payload_digest_mismatch"
    )
    other = _row(payload)
    moved = FactOutboxRow(**{**other.__dict__, "aggregate_id": uuid4()})
    assert _check(payload, outbox=moved) == "outbox_identity_mismatch"


def test_binding_status_follows_the_plan_line_net_of_restorations() -> None:
    from src.services.credit_fact_consumer import _CreditLine

    def line(reversed_cents: int, restored: int) -> Any:
        return _CreditLine("plan", 5_000_000, 99_900, reversed_cents, restored)

    assert binding_status(line(0, 0)) == "active"
    assert binding_status(line(20_000, 0)) == "active"
    assert binding_status(line(99_900, 0)) == "reversed"
    assert binding_status(line(99_900, 1)) == "active"
    assert binding_status(line(99_900, 99_900)) == "active"


# ---------------------------------------------------------------------------
# The entry point's guards: nothing is opened or written when they refuse.
# ---------------------------------------------------------------------------


def _no_session() -> AsyncSession:
    raise AssertionError("the consumer must not open a session")


async def test_presentation_guard_raises_before_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inverted pin (asaas-rollout-opening Group 2c): the guard is closed by
    # default, so with the switch off the first refusal is CreditSwitchOff,
    # still before any session.
    assert outbox_module.PRESENTATION_PRECONDITIONS_OPEN is False
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: False)
    with pytest.raises(CreditSwitchOff):
        await CreditFactConsumer(_no_session).handle(_fixture(TOPUP[0]))
    with pytest.raises(CreditSwitchOff):
        await handle_billing_fact(_fixture(TOPUP[0]))
    # Reopened, the guard refuses first again, with the switch on and with
    # it off: the guard is checked before the switch.
    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", True)
    for effective in (True, False):
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self, e=effective: e)
        with pytest.raises(PresentationPreconditionsOpen):
            await CreditFactConsumer(_no_session).handle(_fixture(TOPUP[0]))
        with pytest.raises(PresentationPreconditionsOpen):
            await handle_billing_fact(_fixture(TOPUP[0]))


async def test_the_switch_must_be_effective(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", False)
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: False)
    with pytest.raises(CreditSwitchOff):
        await CreditFactConsumer(_no_session).handle(_fixture(TOPUP[0]))


async def test_a_divergence_raises_before_opening_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", False)
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)
    monkeypatch.setattr(consumer_module, "billing_fact_schema_digest", lambda: "f" * 64)
    with pytest.raises(ConsumerContractDivergence):
        await CreditFactConsumer(_no_session).handle(_fixture(TOPUP[0]))


def test_handle_billing_fact_is_a_fact_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signature = inspect.signature(handle_billing_fact)
    assert list(signature.parameters) == ["payload"]
    assert inspect.iscoroutinefunction(handle_billing_fact)
    config = BillingFactWorkerConfig(
        interval_seconds=30, batch_size=1, max_attempts=1, stale_after_seconds=60
    )
    sessions = async_sessionmaker(class_=AsyncSession)
    # Inverted pin (asaas-rollout-opening Group 2c): with the guard closed by
    # default the worker takes the consumer as its transport.
    assert outbox_module.PRESENTATION_PRECONDITIONS_OPEN is False
    worker = BillingFactWorker(sessions, transport=handle_billing_fact, config=config)
    assert worker.consumer_ready
    # Reopened, the worker refuses it again.
    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", True)
    with pytest.raises(PresentationPreconditionsOpen):
        BillingFactWorker(sessions, transport=handle_billing_fact, config=config)
    assert CreditFactConsumer()._timeout == TRANSPORT_TIMEOUT_SECONDS == 10.0


@pytest.mark.usefixtures("consumer_on")
async def test_the_call_ends_within_its_budget_and_writes_nothing() -> None:
    closed: list[bool] = []

    class _Hanging:
        async def connection(self) -> None:
            await asyncio.Event().wait()

        async def rollback(self) -> None:
            return None

        async def close(self) -> None:
            closed.append(True)

    consumer = CreditFactConsumer(lambda: _Hanging(), timeout_seconds=0.05)  # type: ignore[arg-type,return-value]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(consumer.handle(_fixture(TOPUP[0])), timeout=5)
    assert closed == [True]


# ---------------------------------------------------------------------------
# Inbox, ack, ledger and binding (SQLite).
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("consumer_on")
async def test_the_six_fixtures_replay_into_inbox_acks_lots_and_binding(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    consumer = _consumer(sessions)
    payloads = [_remapped(name) for name in CYCLE + TOPUP]
    for payload in payloads:
        await _seed(sessions, payload)
    outcomes = [(await consumer.handle(p)).outcome for p in payloads]
    assert outcomes == ["accepted"] * 6
    replays = [(await consumer.handle(p)).outcome for p in payloads]
    assert replays == ["replay"] * 6
    assert await _count(sessions, CreditFactInbox) == 6
    assert await _count(sessions, BillingFactAck) == 6

    async with sessions() as session:
        acks = {
            ack.event_id: ack
            for ack in (await session.execute(select(BillingFactAck))).scalars()
        }
        inbox = {
            row.event_id: row
            for row in (await session.execute(select(CreditFactInbox))).scalars()
        }
        for payload in payloads:
            event_id = UUID(payload["event_id"])
            ack, row = acks[event_id], inbox[event_id]
            assert ack.outcome == row.outcome == "accepted"
            assert ack.schema_version == "billing.fact.ack.v1"
            assert ack.fact_sha256 == row.fact_sha256 == payload_digest(payload)
            assert ack.bundle_digest == row.bundle_digest == PINNED_BUNDLE_DIGEST
            assert row.schema_digest == PINNED_SCHEMA_DIGEST
            assert ack.deployment_digest == DEPLOYMENT
            assert ack.inbox_event_id == row.id
            assert row.financial_revision == payload["financial_revision"]
        lots = {
            lot.lot_kind: lot
            for lot in (await session.execute(select(CreditLot))).scalars()
        }
        assert lots["monthly"].granted_mc == 5_000_000
        assert lots["topup"].granted_mc == 500_000
        assert lots["monthly"].expires_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 11, 12, 3, 0, tzinfo=timezone.utc
        )
        assert lots["topup"].expires_at.replace(tzinfo=timezone.utc) == datetime(
            2027, 4, 12, 3, 0, tzinfo=timezone.utc
        )
        (binding,) = (await session.execute(select(CreditEnforcementBinding))).scalars()
        assert (binding.status, binding.financial_revision) == ("active", 3)
        assert binding.subscription_cycle_id == UUID(payloads[0]["aggregate_id"])
        entries = (
            await session.execute(
                select(CreditLedgerEntry.entry_type, CreditLedgerEntry.amount_mc)
            )
        ).all()
        assert sorted((t, int(a)) for t, a in entries) == [
            ("grant", 500_000),
            ("grant", 5_000_000),
            ("lot_regrant", 125_000),
            ("reversal", 250_000),
        ]
        # The settled cycle's ack covers its grant effect; the clinic-only
        # reversal/restoration produced no effect.
        cycle_settled = acks[UUID(payloads[0]["event_id"])]
        assert cycle_settled.effects_digest == effects_digest(
            [lots["monthly"].grant_effect_id]
        )
        for payload in payloads[1:3]:
            assert acks[UUID(payload["event_id"])].effects_digest == (
                EMPTY_EFFECTS_DIGEST
            )


@pytest.mark.usefixtures("consumer_on")
async def test_a_revision_jump_opens_a_gap_and_the_late_one_is_a_no_op(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    consumer = _consumer(sessions)
    settled, reversed_, restored = (_remapped(name) for name in TOPUP)
    for payload in (settled, reversed_, restored):
        await _seed(sessions, payload)
    assert (await consumer.handle(settled)).outcome == "accepted"
    assert (await consumer.handle(restored)).outcome == "accepted"
    async with sessions() as session:
        (gap,) = (await session.execute(select(CreditRevisionGap))).scalars()
        assert (gap.first_missing_revision, gap.closed_at) == (2, None)
    late = await consumer.handle(reversed_)
    assert (late.outcome, late.effect_ids) == ("stale", ())
    async with sessions() as session:
        (gap,) = (await session.execute(select(CreditRevisionGap))).scalars()
        assert gap.closed_at is not None
        late_ack = (
            await session.execute(
                select(BillingFactAck).where(
                    BillingFactAck.event_id == UUID(reversed_["event_id"])
                )
            )
        ).scalar_one()
        assert (late_ack.outcome, late_ack.effects_digest) == (
            "accepted",
            EMPTY_EFFECTS_DIGEST,
        )
        (lot,) = (await session.execute(select(CreditLot))).scalars()
        balance = sum(
            int(a) * (1 if t in ("grant", "lot_regrant") else -1)
            for t, a in (
                await session.execute(
                    select(CreditLedgerEntry.entry_type, CreditLedgerEntry.amount_mc)
                )
            ).all()
        )
        assert balance == 375_000  # the restored snapshot applied once


@pytest.mark.usefixtures("consumer_on")
async def test_a_tampered_fact_gets_inbox_and_rejected_ack_from_the_outbox_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(TOPUP[0])
    await _seed(sessions, payload)
    tampered = copy.deepcopy(payload)
    tampered["credit_grant_mc"] = tampered["lines"][0]["credit_grant_mc"] = 9_000_000
    tampered["aggregate_id"] = tampered["order_id"] = str(uuid4())
    handling = await _consumer(sessions).handle(tampered)
    assert (handling.outcome, handling.reason) == (
        "rejected",
        "payload_digest_mismatch",
    )
    assert await _count(sessions, CreditEffect) == 0
    assert await _count(sessions, CreditLot) == 0
    async with sessions() as session:
        (row,) = (await session.execute(select(CreditFactInbox))).scalars()
        (ack,) = (await session.execute(select(BillingFactAck))).scalars()
    assert row.outcome == ack.outcome == "rejected"
    assert row.aggregate_id == UUID(
        payload["aggregate_id"]
    )  # the row's, not the payload's
    assert ack.fact_sha256 == payload_digest(tampered) != payload_digest(payload)
    assert ack.effects_digest == EMPTY_EFFECTS_DIGEST
    # Replaying the same bytes changes nothing; the genuine bytes now collide.
    assert (await _consumer(sessions).handle(tampered)).outcome == "replay"
    with pytest.raises(FactCollision):
        await _consumer(sessions).handle(payload)
    assert await _count(sessions, BillingFactAck) == 1


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (_partial_credit_line, "partial_credit_line"),
        (_ambiguous_allocation, "ambiguous_line_allocation"),
    ],
)
@pytest.mark.usefixtures("consumer_on")
async def test_closed_invariant_violations_are_rejected_with_zero_effects(
    sessions: async_sessionmaker[AsyncSession], build: Any, reason: str
) -> None:
    payload = build()
    for field_name in _ID_FIELDS:
        if isinstance(payload.get(field_name), str):
            payload[field_name] = _sqlite_safe(payload[field_name])
    await _seed(sessions, payload)
    first = await _consumer(sessions).handle(payload)
    again = await _consumer(sessions).handle(payload)
    assert (first.outcome, first.reason, again.outcome) == (
        "rejected",
        reason,
        "replay",
    )
    assert await _count(sessions, CreditEffect) == 0
    assert await _count(sessions, CreditEnforcementBinding) == 0
    async with sessions() as session:
        (ack,) = (await session.execute(select(BillingFactAck))).scalars()
    assert (ack.outcome, ack.effects_digest) == ("rejected", EMPTY_EFFECTS_DIGEST)


@pytest.mark.usefixtures("consumer_on")
async def test_a_consumer_divergence_writes_nothing_and_a_fixed_deploy_applies(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _remapped(TOPUP[0])
    await _seed(sessions, payload)

    def unavailable() -> str:
        raise BillingFactViolation("billing fact bundle manifest is unavailable")

    with monkeypatch.context() as patched:
        patched.setattr(consumer_module, "billing_fact_bundle_digest", unavailable)
        with pytest.raises(ConsumerContractDivergence):
            await _consumer(sessions).handle(payload)
    assert await _count(sessions, CreditFactInbox) == 0
    assert await _count(sessions, BillingFactAck) == 0
    assert (await _consumer(sessions).handle(payload)).outcome == "accepted"


@pytest.mark.usefixtures("consumer_on")
async def test_a_handoff_with_another_bundle_is_a_consumer_side_divergence(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(TOPUP[0])
    outbox_id = await _seed(sessions, payload)
    async with sessions() as session, session.begin():
        handoff = (
            await session.execute(
                select(BillingFactHandoff).where(
                    BillingFactHandoff.outbox_event_id == outbox_id
                )
            )
        ).scalar_one()
        handoff.bundle_digest = "1" * 64  # SQLite: no immutability trigger
    with pytest.raises(ConsumerContractDivergence):
        await _consumer(sessions).handle(payload)
    assert await _count(sessions, CreditFactInbox) == 0


@pytest.mark.usefixtures("consumer_on")
async def test_an_unknown_event_or_a_revision_collision_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(TOPUP[0])
    with pytest.raises(UnresolvableFact):
        await _consumer(sessions).handle(payload)
    with pytest.raises(UnresolvableFact):
        await _consumer(sessions).handle({**payload, "event_id": "not-a-uuid"})
    await _seed(sessions, payload)
    async with sessions() as session, session.begin():
        session.add(
            CreditFactInbox(
                producer="asaas-payment-gateway",
                event_id=uuid4(),
                schema_version="billing.fact.v1",
                aggregate_kind="topup_order",
                aggregate_id=UUID(payload["aggregate_id"]),
                financial_revision=1,
                fact_sha256="a" * 64,
                bundle_digest="a" * 64,
                schema_digest="a" * 64,
                outcome="accepted",
            )
        )
    with pytest.raises(FactCollision):
        await _consumer(sessions).handle(payload)
    assert await _count(sessions, BillingFactAck) == 0


@pytest.mark.usefixtures("consumer_on")
async def test_a_second_cycle_of_one_period_raises_without_ack_or_binding(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = _remapped(CYCLE[0])
    second = _fresh(first, aggregate_id=uuid4())
    second["payment_id"] = "pay_subscription_duplicate"
    await _seed(sessions, first)
    await _seed(sessions, second)
    assert (await _consumer(sessions).handle(first)).outcome == "accepted"
    with pytest.raises(CycleIdentityConflict):
        await _consumer(sessions).handle(second)
    assert await _count(sessions, BillingFactAck) == 1
    assert await _count(sessions, CreditEnforcementBinding) == 1
    assert await _count(sessions, CreditLot) == 1


@pytest.mark.usefixtures("consumer_on")
async def test_a_chargeback_of_the_plan_line_reverses_then_restores_the_binding(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    settled = _remapped(CYCLE[0])
    reversed_ = _fresh(settled)
    reversed_["financial_revision"] = 2
    reversed_["event_type"] = "subscription.cycle_reversed.v1"
    for line in reversed_["lines"]:
        line["reversed_cents"] = line["settled_cents"]
        line["financial_state"] = "reversed"
    reversed_["reversed_cents"] = reversed_["settled_cents"]
    restored = _fresh(reversed_)
    restored["financial_revision"] = 3
    restored["event_type"] = "subscription.cycle_restored.v1"
    for line in restored["lines"]:
        line["restored_cents"] = line["reversed_cents"]
        line["financial_state"] = "restored"
    restored["restored_cents"] = restored["reversed_cents"]
    consumer = _consumer(sessions)
    statuses = []
    for payload in (settled, reversed_, restored):
        await _seed(sessions, payload)
        assert (await consumer.handle(payload)).outcome == "accepted"
        async with sessions() as session:
            (binding,) = (
                await session.execute(select(CreditEnforcementBinding))
            ).scalars()
            statuses.append((binding.status, binding.financial_revision))
    assert statuses == [("active", 1), ("reversed", 2), ("active", 3)]
    async with sessions() as session:
        totals = dict(
            (
                await session.execute(
                    select(
                        CreditLedgerEntry.entry_type,
                        func.sum(CreditLedgerEntry.amount_mc),
                    ).group_by(CreditLedgerEntry.entry_type)
                )
            ).all()
        )
    assert totals == {
        "grant": 5_000_000,
        "reversal": 5_000_000,
        "lot_regrant": 5_000_000,
    }


@pytest.mark.usefixtures("consumer_on")
async def test_a_late_fact_for_an_old_cycle_grants_an_already_expired_lot(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(TOPUP[0])
    await _seed(sessions, payload)
    consumer = CreditFactConsumer(
        sessions,
        now=lambda: datetime(2027, 5, 1, tzinfo=timezone.utc),
        reference_clock=_at(datetime(2027, 5, 1, tzinfo=timezone.utc)),
    )
    assert (await consumer.handle(payload)).outcome == "accepted"
    async with sessions() as session:
        totals = dict(
            (
                await session.execute(
                    select(
                        CreditLedgerEntry.entry_type,
                        func.sum(CreditLedgerEntry.amount_mc),
                    ).group_by(CreditLedgerEntry.entry_type)
                )
            ).all()
        )
    assert totals == {"grant": 500_000, "expiry": 500_000}  # expired 2027-04-12


def test_the_group2_modules_import_without_a_tests_tree(tmp_path: Path) -> None:
    """Reading the bundle manifest happens per fact, never at import."""

    from tests.services.test_credit_no_tests_dir import (
        _PROBE,
        _copy_tree,
        _image_sources,
    )

    image = tmp_path / "app"
    for source in _image_sources():
        _copy_tree(_ROOT / source, image / source)
    assert not (image / "tests").exists()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            str(image),
            "src.models",
            "src.services.credit_ledger",
            "src.services.credit_fact_consumer",
            "src.services.credit_reconciliation",
        ],
        cwd=image,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.usefixtures("consumer_on")
async def test_the_clock_is_read_only_once_the_account_lock_is_held(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.repositories.credit import CreditRepository

    events: list[str] = []
    original = CreditRepository.lock_account

    async def lock_account(self: CreditRepository, builder_id: UUID) -> Any:
        events.append("lock")
        return await original(self, builder_id)

    def clock() -> datetime:
        events.append("clock")
        return T0

    monkeypatch.setattr(CreditRepository, "lock_account", lock_account)
    payload = _remapped(TOPUP[0])
    await _seed(sessions, payload)
    await CreditFactConsumer(sessions, now=clock, reference_clock=_at(T0)).handle(
        payload
    )
    assert events.index("lock") < events.index("clock")


@pytest.mark.usefixtures("consumer_on")
async def test_the_ack_names_only_the_fact_s_own_effects(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = _remapped(TOPUP[0])  # paid 2026-10-12, expires 2027-04-12
    await _seed(sessions, first)
    await _consumer(sessions).handle(first)
    order = uuid4()
    second = _fresh(first, aggregate_id=order)
    second["paid_at"] = "2027-04-20"
    await _seed(sessions, second)
    late = datetime(2027, 4, 21, 12, 0, tzinfo=timezone.utc)
    handled = await CreditFactConsumer(
        sessions, now=lambda: late, reference_clock=_at(late)
    ).handle(second)
    assert handled.effect_ids == (f"topup:{order}:grant",)
    async with sessions() as session:
        ack = (
            await session.execute(
                select(BillingFactAck).where(
                    BillingFactAck.event_id == UUID(second["event_id"])
                )
            )
        ).scalar_one()
        kinds = sorted(
            (await session.execute(select(CreditEffect.effect_kind))).scalars()
        )
    assert ack.effects_digest == effects_digest([f"topup:{order}:grant"])
    assert kinds == ["expiry", "grant", "grant"]  # the sweep's expiry persisted


def test_the_clock_tolerance_setting_defaults_to_five_minutes_within_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import ValidationError

    from src.config.settings import Settings

    monkeypatch.delenv("CREDIT_CLOCK_TOLERANCE_SECONDS", raising=False)
    assert Settings(_env_file=None).credit_clock_tolerance_seconds == 300
    for value in ("1", "600"):
        monkeypatch.setenv("CREDIT_CLOCK_TOLERANCE_SECONDS", value)
        assert Settings(_env_file=None).credit_clock_tolerance_seconds == int(value)
    for value in ("0", "-1", "601", "3600", "1.5", "five"):
        monkeypatch.setenv("CREDIT_CLOCK_TOLERANCE_SECONDS", value)
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


async def _stamps(sessions: async_sessionmaker[AsyncSession]) -> set[datetime]:
    """Every instant the consumer wrote: ledger, inbox, acks, gaps."""

    from src.models.credit import CreditLedgerEntry

    found: set[datetime] = set()
    async with sessions() as session:
        for column in (
            CreditLedgerEntry.occurred_at,
            CreditFactInbox.received_at,
            BillingFactAck.acknowledged_at,
            CreditRevisionGap.opened_at,
            CreditRevisionGap.closed_at,
        ):
            for value in (await session.execute(select(column))).scalars():
                if value is not None:
                    found.add(
                        value if value.tzinfo else value.replace(tzinfo=timezone.utc)
                    )
    return found


@pytest.mark.usefixtures("consumer_on")
async def test_the_consumer_uses_the_configured_clock_tolerance(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.config.settings import settings
    from src.services.credit_ledger import CreditLedger, LedgerClockAhead

    reference = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

    async def database_clock() -> datetime:
        return reference

    eight_minutes_ahead = reference + timedelta(minutes=8)
    payload = _remapped(TOPUP[0])
    await _seed(sessions, payload)
    monkeypatch.setattr(settings, "credit_clock_tolerance_seconds", 300)
    handled = await CreditFactConsumer(
        sessions, now=lambda: eight_minutes_ahead, reference_clock=database_clock
    ).handle(payload)
    assert handled.outcome == "accepted"
    assert await _stamps(sessions) == {reference + timedelta(minutes=5)}
    monkeypatch.setattr(settings, "credit_clock_tolerance_seconds", 600)
    async with sessions() as session:
        assert CreditLedger(session).clock_tolerance == timedelta(minutes=10)
    wider = _fresh(payload, aggregate_id=uuid4())
    await _seed(sessions, wider)
    await CreditFactConsumer(
        sessions, now=lambda: eight_minutes_ahead, reference_clock=database_clock
    ).handle(wider)
    assert eight_minutes_ahead in await _stamps(sessions)
    # The constructor parameter still overrides the setting (tests): with 5
    # minutes, this builder's ledger clock (8 minutes ahead) is beyond the
    # bound -- the one refusal left, writing nothing.
    another = _fresh(payload, aggregate_id=uuid4())
    await _seed(sessions, another)
    before = await _stamps(sessions), await _count(sessions, BillingFactAck)
    with pytest.raises(LedgerClockAhead, match="ledger clock"):
        await CreditFactConsumer(
            sessions,
            now=lambda: reference,
            reference_clock=database_clock,
            clock_tolerance=timedelta(minutes=5),
        ).handle(another)
    assert (await _stamps(sessions), await _count(sessions, BillingFactAck)) == before


@pytest.mark.usefixtures("consumer_on")
async def test_a_clock_ahead_is_limited_on_every_path(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Owner decision C: an application clock far ahead never refuses a fact;
    applied, rejected and older-revision facts are all written within the
    tolerance of the database clock."""

    ahead = T0 + timedelta(days=200)
    limit = T0 + timedelta(minutes=5)
    early = CreditFactConsumer(sessions, now=lambda: ahead, reference_clock=_at(T0))
    settled, reversed_, restored = (_remapped(name) for name in TOPUP)
    for payload in (settled, reversed_, restored):
        await _seed(sessions, payload)
    other = _fresh(settled, aggregate_id=uuid4())
    await _seed(sessions, other)
    tampered = copy.deepcopy(other)
    tampered["credit_grant_mc"] = 1
    assert (await early.handle(tampered)).outcome == "rejected"
    assert (await early.handle(settled)).outcome == "accepted"
    assert (await early.handle(restored)).outcome == "accepted"  # gap at rev 2
    assert (await early.handle(reversed_)).outcome == "stale"  # acked, closes it
    assert await _count(sessions, BillingFactAck) == 4
    async with sessions() as session:
        (gap,) = (await session.execute(select(CreditRevisionGap))).scalars()
    assert gap.closed_at is not None
    assert await _stamps(sessions) == {limit}
    # A replay is still a replay.
    assert (await early.handle(settled)).outcome == "replay"
    assert await _count(sessions, BillingFactAck) == 4
