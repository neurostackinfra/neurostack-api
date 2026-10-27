"""Read-only reconciliation of the credit consumer (Group 2, Deliverable 6).

Each signal of :class:`CreditReconciliationReport` turns the readiness
false, and the evaluation itself never writes: no inbox row, effect, ack or
ASAAS row appears, whatever it finds.
"""

from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.base import Base
from src.models.billing import (
    BillingFactAck,
    BillingPaymentEffect,
    BillingSubscriptionCycle,
    BillingTopupOrder,
)
from src.services.credit_reconciliation import (
    CreditReconciliation,
    CreditReconciliationReport,
    credit_reconciliation_ready,
)
from tests.services import test_credit_fact_consumer as consumer_suite
from tests.services.test_credit_fact_consumer import (
    CYCLE,
    TOPUP,
    _consumer,
    _fresh,
    _remapped,
    _seed,
)

# The consumer suite's fixtures, shared as they are.
sessions = consumer_suite.sessions
inbox_seq = consumer_suite.inbox_seq
consumer_on = consumer_suite.consumer_on


async def _report(
    sessions: async_sessionmaker[AsyncSession],
) -> CreditReconciliationReport:
    async with sessions() as session:
        report = await CreditReconciliation(session).report()
        assert not session.new and not session.dirty and not session.deleted
        assert await credit_reconciliation_ready(session) is report.ready
        return report


async def _row_counts(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sessions() as session:
        return {
            table.name: int(
                (
                    await session.execute(select(func.count()).select_from(table))
                ).scalar_one()
            )
            for table in Base.metadata.sorted_tables
            if table.name.startswith(("credit_", "billing_"))
        }


def _cycle_row(payload: dict[str, Any], **values: Any) -> BillingSubscriptionCycle:
    period = payload["subscription_cycle"]
    return BillingSubscriptionCycle(
        id=UUID(payload["aggregate_id"]),
        builder_id=UUID(payload["builder_id"]),
        subscription_id=UUID(payload["subscription_id"]),
        payment_id=payload["payment_id"],
        catalog_version_id=payload["catalog_version_id"],
        offer_id=payload["offer_id"],
        cycle_start=date.fromisoformat(period["cycle_start"]),
        cycle_end=date.fromisoformat(period["cycle_end"]),
        amount_cents=payload["total_amount_cents"],
        credit_grant_mc=payload["credit_grant_mc"],
        pricing_snapshot={},
        financial_snapshot={},
        **values,
    )


async def test_an_empty_world_is_ready(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    report = await _report(sessions)
    assert report.ready and report == CreditReconciliationReport()
    assert not report.has_backlog and not report.has_open_gap


@pytest.mark.usefixtures("consumer_on")
async def test_an_unacked_fact_is_backlog_until_the_consumer_takes_it(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(TOPUP[0])
    await _seed(sessions, payload)
    before = await _row_counts(sessions)
    report = await _report(sessions)
    assert (report.facts_without_ack, report.facts_without_inbox) == (1, 1)
    assert report.has_backlog and not report.ready
    # Detection never ingests, presents or acknowledges anything.
    assert await _row_counts(sessions) == before
    await _consumer(sessions).handle(payload)
    assert (await _report(sessions)).ready


@pytest.mark.usefixtures("consumer_on")
async def test_a_rejected_fact_stays_open_until_a_later_revision_is_accepted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    settled = _remapped(TOPUP[0])
    await _seed(sessions, settled)
    tampered = copy.deepcopy(settled)
    tampered["credit_grant_mc"] = tampered["lines"][0]["credit_grant_mc"] = 1
    assert (await _consumer(sessions).handle(tampered)).outcome == "rejected"
    report = await _report(sessions)
    assert (report.rejected_open, report.facts_without_ack) == (1, 0)
    assert not report.ready
    later = _remapped(TOPUP[1])
    await _seed(sessions, later)
    assert (await _consumer(sessions).handle(later)).outcome == "accepted"
    assert (await _report(sessions)).ready


@pytest.mark.usefixtures("consumer_on")
async def test_an_open_revision_gap_keeps_readiness_false(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    settled, reversed_, restored = (_remapped(name) for name in TOPUP)
    for payload in (settled, restored):
        await _seed(sessions, payload)
        await _consumer(sessions).handle(payload)
    report = await _report(sessions)
    assert report.open_revision_gaps == 1 and report.has_open_gap
    await _seed(sessions, reversed_)
    await _consumer(sessions).handle(reversed_)
    assert (await _report(sessions)).ready


async def test_quarantine_and_effects_without_outbox_keep_readiness_false(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(CYCLE[0])
    async with sessions() as session, session.begin():
        session.add(_cycle_row(payload, state="quarantined"))
        session.add(
            BillingTopupOrder(
                id=uuid4(),
                builder_id=UUID(payload["builder_id"]),
                payment_method="pix",
                catalog_version_id="payment-2026-10-03",
                offer_id="package_credits_500_v1",
                amount_cents=10_000,
                credit_grant_mc=500_000,
                pricing_snapshot={},
                financial_snapshot={},
                state="quarantined",
            )
        )
        session.add(
            BillingPaymentEffect(
                id=uuid4(),
                builder_id=UUID(payload["builder_id"]),
                projection_id=uuid4(),
                delivery_id=uuid4(),
                provider="asaas",
                payment_id="pay_without_fact",
                effect_kind="settlement",
                source_revision="settlement:v1",
                amount_cents=10_000,
            )
        )
    report = await _report(sessions)
    assert (report.quarantined_lines, report.effects_without_outbox) == (2, 1)
    assert report.binding_lag == 0  # a quarantined cycle is not a lag
    assert report.has_open_gap and not report.ready


@pytest.mark.usefixtures("consumer_on")
async def test_a_settled_cycle_ahead_of_its_binding_is_a_lag(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(CYCLE[0])
    async with sessions() as session, session.begin():
        session.add(
            _cycle_row(
                payload, state="settled", financial_revision=1, settled_cents=119_900
            )
        )
    await _seed(sessions, payload)
    assert (await _report(sessions)).binding_lag == 1
    await _consumer(sessions).handle(payload)
    assert (await _report(sessions)).ready


@pytest.mark.usefixtures("consumer_on")
async def test_an_accepted_ack_with_a_foreign_digest_is_counted(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    payload = _remapped(TOPUP[0])
    await _seed(sessions, payload)
    await _consumer(sessions).handle(payload)
    other = _fresh(_remapped(TOPUP[0]), aggregate_id=uuid4())
    outbox_id = await _seed(sessions, other)
    async with sessions() as session, session.begin():
        session.add(
            BillingFactAck(
                id=uuid4(),
                outbox_event_id=outbox_id,
                event_id=outbox_id,
                schema_version="billing.fact.ack.v1",
                outcome="accepted",
                inbox_event_id=uuid4(),
                fact_sha256="0" * 64,
                bundle_digest="0" * 64,
                deployment_digest="0" * 64,
                effects_digest="0" * 64,
                acknowledged_at=datetime.now(timezone.utc),
            )
        )
    report = await _report(sessions)
    assert (report.digest_mismatches, report.facts_without_inbox) == (1, 1)
    assert not report.ready
