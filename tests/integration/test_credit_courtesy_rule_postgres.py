"""The courtesy rule on real PostgreSQL (debito-cortesia-automatico, Group 1).

Every proof gets a fresh database copied from one migrated template (head,
047: the ``template``/``clean`` fixtures of the ASAAS fact-worker suite),
with this process's credit switch forced effective on a head reading (both
the 046 courtesy gate and the 047 mark gate open) unless a proof pins
another revision.  Proven here:

* the rule (DESIGN items 1-2, Decision 13): a builder with no global and no
  mark that ever received courtesy is ``courtesy``/``charged`` while its
  courtesy spendable is positive, ``courtesy``/``blocked`` once it is 0 --
  spent, removed or expired -- with no paid plan in force, and ``none``
  (protected) with one; ``at`` is the first grant's ``occurred_at``;
  equality is ``(origin, at, state)``; below 046 the rule reads nothing;
  global > mark > courtesy; the rule is ONE statement, and the paid plan's
  one more only once the courtesy is spent (wish Decision 8);
* the paid plan in force (wish Decisions 3 and 5): an active subscription
  inside the binding grace protects even under a quarantine (never
  ``binding_coverage``); a top-up with nothing left but a future expiry
  protects; an inactive or cancelled subscription has no grace; a paid
  current cycle without a binding protects, a reversed or pending one not;
* the automatic unblock (DESIGN item 6, Risk 7): a new grant, or a paid
  plan, turns ``blocked`` into ``charged``/``none``; with old open debt the
  grant settles it first;
* the debit by the captured mode (DESIGN item 3, Decisions 4, 14, 17):
  only courtesy lots, FEFO among them, the last one in part, the rest
  forgiven -- no ``credit_debts`` row, no settlement, no ``monthly`` or
  ``topup`` entry -- under an ASAAS quarantine, and never re-read (a mark
  recorded after the capture changes nothing);
* the windows and storage (wish Decision 6): the window of ``at`` opens as a
  free baseline, the next one as maintenance paid by courtesy only; what
  the courtesy cannot pay stays outside the coverage and the next
  reservation bills the gap; a charge the courtesy pays nothing of records
  ``charged_mc = 0`` with no effect; egress above the included amount is
  paid by courtesy only; the reservation of a builder under the rule skips
  the binding coverage and the hold; a stale courtesy capture (``charged``
  after "Tirar tudo", ``blocked`` after a grant) is refused by
  ``_enforced_book``'s re-check under the account lock
  (``CreditEnforcementChanged``) with no allocation, effect or entry;
* the lifecycle (DESIGN item 4): no episode is opened, transitioned or
  reactivated under the rule; the 50/90/100 thresholds run on the courtesy
  basis under ``c:{grant_effect_id}`` of the latest grant, a removal is
  never usage; ``request_gate`` and the worker are inert.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.models.credit import CreditEffect, CreditLedgerEntry
from src.models.credit_inventory import CreditAllocation
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.repositories.credit import NOT_ENFORCED, CreditRepository, EffectiveEnforcement
from src.services import credit_inventory, credit_lifecycle
from src.services.credit_ledger import (
    CreditLedger,
    add_calendar_months,
    civil_date,
    start_of_civil_day,
)
from src.services.credit_metering import CreditEnforcementChanged
from src.services.credit_rollout import CreditRolloutService
from src.services.media_assets import media_payload_sha256
from tests.integration.credit_rollout_world import (
    COURTESY_RULE_READ,
    MARK_ACTOR,
    MARK_REASON,
    PAID_PLAN_READ,
    civil_today,
    clean,
    courtesy_captured,
    debt_rows,
    effective_of,
    execute,
    force_credit_switch,
    grant_courtesy,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_tenant,
    make_topup,
    paid_lot_moves,
    plant,
    record_mark,
    remove_courtesy,
    scalar,
    set_subscription_status,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

MC = 1_000


@pytest.fixture(autouse=True)
def _switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The credit switch effective on a head reading (046 and 047 open)."""

    force_credit_switch(monkeypatch)


@dataclass
class _Clock:
    now: datetime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """The ledger's database clock and the inventory's clock, simulated: the
    window proofs live on chosen civil dates (after the real one)."""

    current = _Clock(datetime.now(timezone.utc))

    async def reference_now(_self: CreditLedger) -> datetime:
        return current.now

    monkeypatch.setattr(CreditLedger, "reference_now", reference_now)
    monkeypatch.setattr(credit_inventory, "_now", lambda: current.now)
    return current


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _midday(day: date) -> datetime:
    return start_of_civil_day(day) + timedelta(hours=15)


async def _debit(
    harness: _Harness,
    builder_id: UUID,
    amount: int,
    *,
    captured: EffectiveEnforcement | None,
    at: datetime | None = None,
    effect_id: str | None = None,
) -> Any:
    async with harness.sessions() as session, session.begin():
        return await CreditLedger(session).debit(
            builder_id,
            effect_id or f"trace:{uuid4()}:decision:1",
            amount,
            at=at or datetime.now(timezone.utc),
            effect_kind="trace",
            captured=captured,
        )


async def _entries(
    harness: _Harness, builder_id: UUID, effect_id: str
) -> list[tuple[str, UUID | None, int]]:
    async with harness.sessions() as session:
        rows = await session.execute(
            select(
                CreditLedgerEntry.entry_type,
                CreditLedgerEntry.lot_id,
                CreditLedgerEntry.amount_mc,
            ).where(
                CreditLedgerEntry.builder_id == builder_id,
                CreditLedgerEntry.effect_id == effect_id,
            )
        )
        # One effect's entries share their transaction's instant: compared
        # by type, then the largest amount first (never by the random id).
        return sorted(
            ((str(kind), lot, int(amount)) for kind, lot, amount in rows),
            key=lambda entry: (entry[0], -entry[2]),
        )


async def _effect_exists(harness: _Harness, builder_id: UUID, effect_id: str) -> bool:
    async with harness.sessions() as session:
        found = await session.scalar(
            select(CreditEffect.id).where(
                CreditEffect.builder_id == builder_id,
                CreditEffect.effect_id == effect_id,
            )
        )
    return found is not None


async def _allocations(harness: _Harness, builder_id: UUID) -> list[CreditAllocation]:
    async with harness.sessions() as session:
        return list(
            (
                await session.execute(
                    select(CreditAllocation)
                    .where(CreditAllocation.builder_id == builder_id)
                    .order_by(CreditAllocation.window_start, CreditAllocation.id)
                )
            ).scalars()
        )


async def _lifecycle_rows(harness: _Harness, builder_id: UUID) -> tuple[int, int]:
    episodes = await scalar(
        harness,
        "SELECT count(*) FROM credit_lifecycle_episodes WHERE builder_id = :b",
        b=builder_id,
    )
    transitions = await scalar(
        harness,
        "SELECT count(*) FROM credit_lifecycle_transitions WHERE builder_id = :b",
        b=builder_id,
    )
    return int(episodes), int(transitions)


async def _alerts(
    harness: _Harness, builder_id: UUID
) -> list[tuple[str, int, int, int]]:
    async with harness.engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT arm_key, threshold, capacity_mc, available_mc FROM "
                "credit_threshold_alerts WHERE builder_id = :b "
                "ORDER BY occurred_at, threshold"
            ),
            {"b": builder_id},
        )
        return [(str(k), int(t), int(c), int(a)) for k, t, c, a in rows]


def _blocked(at: datetime) -> EffectiveEnforcement:
    return EffectiveEnforcement(at, "courtesy", state="blocked")


def _charged(at: datetime) -> EffectiveEnforcement:
    return EffectiveEnforcement(at, "courtesy", state="charged")


async def _spent_builder(harness: _Harness) -> tuple[UUID, Any]:
    """A builder whose only courtesy was removed ("Tirar tudo"): blocked
    until a paid plan or a new courtesy appears."""

    builder_id = await make_builder(harness)
    granted = await grant_courtesy(harness, builder_id, 5)
    await remove_courtesy(harness, granted.lot_id)
    assert await effective_of(harness, builder_id) == _blocked(granted.occurred_at)
    return builder_id, granted


# ---------------------------------------------------------------------------
# The rule: states, the stable instant, precedence and the gate.
# ---------------------------------------------------------------------------


def test_the_verdict_is_origin_instant_and_state() -> None:
    at = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)
    builder = uuid4()
    charged = EffectiveEnforcement(at, "courtesy", builder_id=builder, state="charged")
    assert charged == _charged(at) and charged.enforced and charged.courtesy
    assert charged != _blocked(at) and _blocked(at).blocked and not charged.blocked
    assert charged != _charged(at + timedelta(seconds=1))
    assert charged != EffectiveEnforcement(at, "mark")
    assert EffectiveEnforcement(None, None, rule_unread=True) == NOT_ENFORCED
    assert not EffectiveEnforcement(at, "mark").courtesy


async def test_the_rule_charges_blocks_and_keeps_the_first_grant_instant(
    clean: _Harness,
) -> None:
    builder_id = await make_builder(clean)
    assert await effective_of(clean, builder_id) == NOT_ENFORCED  # no courtesy
    first = await grant_courtesy(clean, builder_id, 3)
    found = await effective_of(clean, builder_id)
    assert found == _charged(first.occurred_at)
    assert (found.at, found.origin, found.state, found.builder_id) == (
        first.occurred_at,
        "courtesy",
        "charged",
        builder_id,
    )
    second = await grant_courtesy(clean, builder_id, 2)
    assert second.occurred_at > first.occurred_at
    # ``at`` is stable: the FIRST grant, whatever came after.
    assert await effective_of(clean, builder_id) == _charged(first.occurred_at)
    await remove_courtesy(clean, first.lot_id)
    assert await effective_of(clean, builder_id) == _charged(first.occurred_at)
    await remove_courtesy(clean, second.lot_id)
    blocked = await effective_of(clean, builder_id)
    assert blocked == _blocked(first.occurred_at) and blocked.blocked
    # DESIGN item 6: a new courtesy with a balance unblocks, no operator.
    third = await grant_courtesy(clean, builder_id, 1)
    assert await effective_of(clean, builder_id) == _charged(first.occurred_at)
    # DESIGN Decision 7: an expired courtesy with balance blocks like a spent one.
    after = third.expires_at + timedelta(hours=1)
    assert await effective_of(clean, builder_id, at=after) == _blocked(
        first.occurred_at
    )
    # A builder that never had courtesy stays exactly as before.
    other = await make_builder(clean)
    assert await effective_of(clean, other, at=after) == NOT_ENFORCED


async def test_global_then_mark_then_courtesy(clean: _Harness) -> None:
    builder_id = await make_builder(clean)
    granted = await grant_courtesy(clean, builder_id, 2)
    marked_at = await record_mark(clean.engine, builder_id)
    assert await effective_of(clean, builder_id) == EffectiveEnforcement(
        marked_at, "mark"
    )
    await record_mark(clean.engine, builder_id, "unmark")
    assert await effective_of(clean, builder_id) == _charged(granted.occurred_at)
    async with clean.sessions() as session:
        armed = datetime.now(timezone.utc) - timedelta(days=3)
        await session.execute(
            text(
                "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, "
                "set_by, set_at) VALUES (1, :at, 'courtesy-rule-test', :at)"
            ),
            {"at": armed},
        )
        assert await CreditRepository(session).effective_enforcement(builder_id) == (
            EffectiveEnforcement(armed, "global")
        )
        await session.rollback()


async def test_below_046_the_rule_reads_nothing(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id = await make_builder(clean)
    granted = await grant_courtesy(clean, builder_id, 2)
    assert await effective_of(clean, builder_id) == _charged(granted.occurred_at)

    force_credit_switch(monkeypatch, revision="045")
    with _statements(clean.engine) as seen:
        unread = await effective_of(clean, builder_id)
    assert unread == NOT_ENFORCED and unread.rule_unread is True
    assert seen and not any("credit_lots" in s or "billing_" in s for s in seen)
    # The caller's own verdict closes it too, on any reading.
    force_credit_switch(monkeypatch)
    closed = await effective_of(clean, builder_id, rule_readable=False)
    assert closed == NOT_ENFORCED and closed.rule_unread is True

    # At 046 the marks gate is closed (no table) and the rule's is open.
    force_credit_switch(monkeypatch, revision="046")
    with _statements(clean.engine) as seen:
        at_046 = await effective_of(clean, builder_id)
    assert at_046 == _charged(granted.occurred_at) and at_046.mark_unread is True
    assert not any("credit_enforcement_marks" in s for s in seen)


async def test_the_rule_is_one_read_and_the_plan_is_read_only_when_spent(
    clean: _Harness,
) -> None:
    """Wish Decision 8: the rule's one statement for every builder with no
    global and no mark; the paid plan's one more only with nothing spendable;
    a marked builder and the global read nothing new."""

    async def statements_of(builder_id: UUID) -> list[str]:
        with _statements(clean.engine) as seen:
            await effective_of(clean, builder_id)
        return [s for s in seen if "credit_" in s or "billing_" in s]

    plain = await make_builder(clean)
    reads = await statements_of(plain)
    assert len(reads) == 3 and COURTESY_RULE_READ.fullmatch(reads[2].strip())
    assert not any("billing_" in s for s in reads)

    charged = await make_builder(clean)
    await grant_courtesy(clean, charged, 1)
    reads = await statements_of(charged)
    assert len(reads) == 3 and COURTESY_RULE_READ.fullmatch(reads[2].strip())
    assert not any(PAID_PLAN_READ.fullmatch(s.strip()) for s in reads)

    spent, _granted = await _spent_builder(clean)
    reads = await statements_of(spent)
    assert len(reads) == 4 and COURTESY_RULE_READ.fullmatch(reads[2].strip())
    assert PAID_PLAN_READ.fullmatch(reads[3].strip())

    await record_mark(clean.engine, charged)
    reads = await statements_of(charged)
    assert len(reads) == 2 and not any("credit_lots" in s for s in reads)


# ---------------------------------------------------------------------------
# The paid plan in force (wish Decisions 3 and 5).
# ---------------------------------------------------------------------------


async def test_an_active_subscription_in_its_grace_protects_under_quarantine(
    clean: _Harness,
) -> None:
    builder_id, granted = await _spent_builder(clean)
    today = civil_today()
    subscription = await make_subscription(clean, builder_id)
    start, end = today - timedelta(days=31), today - timedelta(days=1)
    cycle = await make_cycle(clean, builder_id, subscription, start=start, end=end)
    await make_monthly_grant(
        clean,
        builder_id=builder_id,
        subscription_id=subscription,
        cycle_id=cycle,
        start=start,
        end=end,
    )
    # The renewal is in quarantine (a cycle of the producer, no binding).
    await make_cycle(
        clean,
        builder_id,
        subscription,
        start=end,
        end=end + timedelta(days=30),
        state="quarantined",
    )
    async with clean.sessions() as session:
        quarantined = await CreditRepository(session).quarantine_hold(builder_id)
        coverage = await CreditRolloutService(session).binding_coverage(builder_id)
        plan = await CreditRepository(session).paid_plan_in_force(builder_id)
        await session.rollback()
    assert quarantined and not coverage.usable  # binding_coverage stops there
    assert not plan.paid_lot  # the allowance expired at its cycle_end
    assert plan.last_binding_cycle_end == end and plan.in_grace and plan.in_force
    assert await effective_of(clean, builder_id) == NOT_ENFORCED  # protected
    assert plan.grace_ends_at is not None
    after = plan.grace_ends_at + timedelta(minutes=1)
    assert await effective_of(clean, builder_id, at=after) == _blocked(
        granted.occurred_at
    )


async def test_a_topup_with_nothing_left_protects_until_it_expires(
    clean: _Harness,
) -> None:
    builder_id, granted = await _spent_builder(clean)
    expires = datetime.now(timezone.utc) + timedelta(days=40)
    await make_topup(clean, builder_id, reversed_mc=500_000, expires_at=expires)
    async with clean.sessions() as session:
        plan = await CreditRepository(session).paid_plan_in_force(builder_id)
        await session.rollback()
    assert plan.paid_lot and not plan.in_grace
    assert await effective_of(clean, builder_id) == NOT_ENFORCED  # balance 0
    assert await effective_of(
        clean, builder_id, at=expires + timedelta(seconds=1)
    ) == _blocked(granted.occurred_at)


@pytest.mark.parametrize("status", ["inactive", "cancelled"])
async def test_an_inactive_or_cancelled_subscription_has_no_grace(
    clean: _Harness, status: str
) -> None:
    builder_id, granted = await _spent_builder(clean)
    today = civil_today()
    subscription = await make_subscription(clean, builder_id)
    start, end = today - timedelta(days=31), today - timedelta(days=1)
    cycle = await make_cycle(clean, builder_id, subscription, start=start, end=end)
    await make_monthly_grant(
        clean,
        builder_id=builder_id,
        subscription_id=subscription,
        cycle_id=cycle,
        start=start,
        end=end,
    )
    assert await effective_of(clean, builder_id) == NOT_ENFORCED  # active: grace
    await set_subscription_status(clean, subscription, status)
    assert await effective_of(clean, builder_id) == _blocked(granted.occurred_at)


@pytest.mark.parametrize(
    ("state", "protected"),
    [("settled", True), ("restored", True), ("reversed", False), ("pending", False)],
)
async def test_a_paid_current_cycle_without_binding_protects(
    clean: _Harness, state: str, protected: bool
) -> None:
    """The fact consumer's lag (owner, wish Decision 5): an active
    subscription whose current cycle is paid but has no binding yet."""

    builder_id, granted = await _spent_builder(clean)
    today = civil_today()
    subscription = await make_subscription(clean, builder_id)
    await make_cycle(
        clean,
        builder_id,
        subscription,
        start=today - timedelta(days=2),
        end=today + timedelta(days=28),
        state=state,
    )
    async with clean.sessions() as session:
        plan = await CreditRepository(session).paid_plan_in_force(builder_id)
        await session.rollback()
    assert plan.unbound_paid_cycle is protected and plan.in_force is protected
    expected = NOT_ENFORCED if protected else _blocked(granted.occurred_at)
    assert await effective_of(clean, builder_id) == expected
    if protected:
        await set_subscription_status(clean, subscription, "cancelled")
        assert await effective_of(clean, builder_id) == _blocked(granted.occurred_at)


async def test_the_current_cycle_is_the_sao_paulo_civil_date_on_both_paths(
    clean: _Harness,
) -> None:
    """``cycle_start <= today < cycle_end`` with ``today`` the civil date in
    São Paulo of the instant judged, never its UTC date: at 22:00 in São
    Paulo (01:00Z the next day) a paid cycle that starts the next civil day
    is not current yet -- blocked -- and three hours later it is --
    protected by the unbound paid cycle -- on the request path
    (``effective_enforcement``/``paid_plan_in_force``) and on the batch
    (``courtesy_rules_for_builders``)."""

    builder_id, granted = await _spent_builder(clean)
    day = civil_today() + timedelta(days=1)  # always after the grant
    subscription = await make_subscription(clean, builder_id)
    await make_cycle(  # settled, with no binding
        clean,
        builder_id,
        subscription,
        start=day + timedelta(days=1),
        end=day + timedelta(days=31),
    )
    late = start_of_civil_day(day) + timedelta(hours=22)
    after = late + timedelta(hours=3)
    # Not vacuous: the UTC date of ``late`` is already the cycle's start.
    assert civil_date(late) == day and late.date() == day + timedelta(days=1)
    assert late.hour == 1 and civil_date(after) == day + timedelta(days=1)

    assert await effective_of(clean, builder_id, at=late) == _blocked(
        granted.occurred_at
    )
    assert await effective_of(clean, builder_id, at=after) == NOT_ENFORCED
    async with clean.sessions() as session:
        try:
            repository = CreditRepository(session)
            single = {
                at: await repository.paid_plan_in_force(builder_id, at=at)
                for at in (late, after)
            }
            batch = {
                at: (await repository.courtesy_rules_for_builders([builder_id], at=at))[
                    builder_id
                ]
                for at in (late, after)
            }
        finally:
            await session.rollback()
    assert not single[late].unbound_paid_cycle and not single[late].in_force
    assert single[after].unbound_paid_cycle and single[after].in_force
    assert batch[late].state == "blocked"
    assert batch[late].paid_plan == single[late]
    assert batch[after].state is None
    assert batch[after].paid_plan == single[after]


async def test_the_batch_answers_a_credit_account_without_a_builders_row(
    clean: _Harness,
) -> None:
    """``credit_accounts.builder_id`` has no foreign key to ``builders``: a
    builder id with courtesy lots and no ``builders`` row is answered by
    the batch exactly as by the single reader (the batch is anchored on the
    accounts, not on ``builders``)."""

    builder_id, granted = await _spent_builder(clean)
    await execute(clean, "DELETE FROM builders WHERE id = :b", b=builder_id)
    assert (
        await scalar(clean, "SELECT count(*) FROM builders WHERE id = :b", b=builder_id)
        == 0
    )
    assert (
        await scalar(
            clean,
            "SELECT count(*) FROM credit_lots WHERE builder_id = :b",
            b=builder_id,
        )
        == 1
    )
    at = datetime.now(timezone.utc)
    async with clean.sessions() as session:
        try:
            repository = CreditRepository(session)
            single = await repository.courtesy_rule(builder_id, at=at)
            batch = await repository.courtesy_rules_for_builders([builder_id], at=at)
        finally:
            await session.rollback()
    assert single.state == "blocked" and single.paid_plan is not None
    assert batch == {builder_id: single}
    assert await effective_of(clean, builder_id, at=at) == _blocked(granted.occurred_at)


# ---------------------------------------------------------------------------
# The automatic unblock (DESIGN item 6, Risk 7).
# ---------------------------------------------------------------------------


async def test_a_grant_or_a_paid_plan_unblocks_and_old_debt_is_settled_first(
    clean: _Harness,
) -> None:
    builder_id, granted = await _spent_builder(clean)
    await make_topup(clean, builder_id)  # a paid plan appears
    assert await effective_of(clean, builder_id) == NOT_ENFORCED

    # Old debt (from before the rule: an unmarked builder's plain debit with
    # no capacity, here written by the ledger itself).
    debtor = await make_builder(clean)
    debt = await _debit(clean, debtor, 2 * MC, captured=None)
    assert debt.debt_mc == 2 * MC
    small = await grant_courtesy(clean, debtor, 1)  # settles 1 credit of it
    assert await effective_of(clean, debtor) == _blocked(small.occurred_at)
    assert await debt_rows(clean, debtor) == (1, 1)
    await grant_courtesy(clean, debtor, 3)  # settles the rest, 2 left
    assert await effective_of(clean, debtor) == _charged(small.occurred_at)
    assert await debt_rows(clean, debtor) == (1, 2)
    async with clean.sessions() as session:
        book = await CreditLedger(session).open_book(
            debtor, at=datetime.now(timezone.utc)
        )
        await session.rollback()
    assert book.open_debt_mc == 0 and book.courtesy_spendable_mc(book.clock) == 2 * MC


# ---------------------------------------------------------------------------
# The debit of the rule, by the captured mode.
# ---------------------------------------------------------------------------


async def test_the_captured_rule_debits_only_courtesy_fefo_under_quarantine(
    clean: _Harness, clock: _Clock
) -> None:
    world = await subscribed_builder(clean)
    builder_id = world.builder_id
    await make_topup(clean, builder_id, state="quarantined")
    start = clock.now
    early = await grant_courtesy(clean, builder_id, 3, at=start)
    clock.now = start + timedelta(days=3)
    late = await grant_courtesy(clean, builder_id, 2, at=clock.now)
    assert early.expires_at < late.expires_at
    clock.now = start + timedelta(days=4)
    captured = courtesy_captured(early.occurred_at)
    async with clean.sessions() as session:
        ledger = CreditLedger(session)
        hold = await ledger.spend_hold(builder_id, captured=captured)
        # The control, outside the rule: the builder marked (rolled back),
        # the request captured on that mark -- the same quarantine refuses.
        await session.execute(
            text(
                "INSERT INTO credit_enforcement_marks (id, builder_id, kind, "
                "occurred_at, actor_user_id, reason) VALUES (:id, :b, 'mark', "
                ":at, :actor, :reason)"
            ),
            {
                "id": uuid4(),
                "b": builder_id,
                "at": start,
                "actor": MARK_ACTOR,
                "reason": MARK_REASON,
            },
        )
        plain = await ledger.spend_hold(
            builder_id, captured=EffectiveEnforcement(start, "mark")
        )
        await session.rollback()
    assert hold.quarantined and not hold.active and hold.courtesy
    assert plain.active and not plain.courtesy
    results = []
    for index, amount in enumerate((1 * MC, 2_500, 3 * MC, 1 * MC)):
        results.append(
            await _debit(
                clean,
                builder_id,
                amount,
                captured=captured,
                at=clock.now,
                effect_id=f"trace:rule:{index}",
            )
        )
    assert [(r.status, r.debited_mc, r.debt_mc, r.forgiven_mc) for r in results] == [
        ("applied", 1_000, 0, 0),
        ("applied", 2_500, 0, 0),
        ("applied", 1_500, 0, 1_500),
        ("applied", 0, 0, 1_000),
    ]
    assert await _entries(clean, builder_id, "trace:rule:0") == [
        ("debit", early.lot_id, 1_000)
    ]
    assert await _entries(clean, builder_id, "trace:rule:1") == [
        ("debit", early.lot_id, 2_000),
        ("debit", late.lot_id, 500),
    ]
    assert await _entries(clean, builder_id, "trace:rule:2") == [
        ("debit", late.lot_id, 1_500)
    ]
    assert await _entries(clean, builder_id, "trace:rule:3") == []
    assert await _effect_exists(clean, builder_id, "trace:rule:3")
    assert await paid_lot_moves(clean, builder_id) == []
    assert await debt_rows(clean, builder_id) == (0, 0)
    assert await _lifecycle_rows(clean, builder_id) == (0, 0)


async def test_the_captured_courtesy_is_never_reread_at_the_debit(
    clean: _Harness,
) -> None:
    """DESIGN Decision 14: the operator marks the builder between the
    capture and the debit -- the debit still pays only courtesy and owes
    nothing; without the capture the same ledger reads the mark and owes."""

    builder_id = await make_builder(clean)
    granted = await grant_courtesy(clean, builder_id, 1)
    captured = courtesy_captured(granted.occurred_at)
    await record_mark(clean.engine, builder_id)
    result = await _debit(clean, builder_id, 3 * MC, captured=captured)
    assert (result.debited_mc, result.debt_mc, result.forgiven_mc) == (MC, 0, 2 * MC)
    assert await debt_rows(clean, builder_id) == (0, 0)
    # The lifecycle follows the book's captured mode too, not the mark it
    # would re-read: no episode for the captured debit.
    assert await _lifecycle_rows(clean, builder_id) == (0, 0)
    marked = await _debit(clean, builder_id, 2 * MC, captured=None)
    assert (marked.debited_mc, marked.debt_mc) == (0, 2 * MC)
    assert (await debt_rows(clean, builder_id))[0] == 1


async def test_a_mark_captured_debit_after_an_unmark_never_takes_the_rule(
    clean: _Harness,
) -> None:
    """The race capture-on-a-mark, unmark, debit: the courtesy rule the
    re-read now finds is not taken for ``enforced`` -- the debit acts as an
    unmarked builder's did before the rule: no quarantine hold, no window
    opening, never courtesy semantics."""

    builder_id = await make_builder(clean)
    await grant_courtesy(clean, builder_id, 2)
    await make_topup(clean, builder_id, state="quarantined")
    marked_at = await record_mark(clean.engine, builder_id)
    captured = EffectiveEnforcement(marked_at, "mark")
    await record_mark(clean.engine, builder_id, "unmark")
    async with clean.sessions() as session:
        hold = await CreditLedger(session).spend_hold(builder_id, captured=captured)
        await session.rollback()
    assert hold.quarantined and not hold.enforced and not hold.courtesy
    assert not hold.active
    result = await _debit(clean, builder_id, MC, captured=captured)
    assert (result.status, result.debited_mc, result.debt_mc) == ("applied", MC, 0)
    assert result.forgiven_mc == 0
    assert await _allocations(clean, builder_id) == []  # no window opening
    assert await _lifecycle_rows(clean, builder_id) == (0, 0)


async def test_without_a_capture_the_rule_is_judged_at_the_books_instant(
    clean: _Harness, clock: _Clock
) -> None:
    """The book's ``at`` (here the ledger clock, 41 days ahead) decides the
    rule, not the wall clock: the top-up that protects the spent builder
    now has expired by then, so the debit is the rule's (forgiven, no
    debt); judged at the wall clock it would be a plain debit owing 1."""

    builder_id, granted = await _spent_builder(clean)
    expires = datetime.now(timezone.utc) + timedelta(days=40)
    await make_topup(clean, builder_id, reversed_mc=500_000, expires_at=expires)
    assert await effective_of(clean, builder_id) == NOT_ENFORCED  # protected now
    clock.now = expires + timedelta(days=1)
    async with clean.sessions() as session:
        hold = await CreditLedger(session).spend_hold(builder_id, at=clock.now)
        await session.rollback()
    assert hold.courtesy_at == granted.occurred_at
    result = await _debit(clean, builder_id, MC, captured=None, at=clock.now)
    assert (result.debited_mc, result.debt_mc, result.forgiven_mc) == (0, 0, MC)
    assert await debt_rows(clean, builder_id) == (0, 0)
    # Only the top-up's own planted reversal: nothing the debit wrote.
    assert await paid_lot_moves(clean, builder_id) == [("topup", "reversal", 500_000)]


async def test_the_rule_never_settles_old_debt_with_a_started_renewal(
    clean: _Harness, clock: _Clock
) -> None:
    """DESIGN item 3 on PostgreSQL: mark-era debt, an early renewal whose
    allowance starts in two days, the unmark; past the cycle start a debit
    captured on the courtesy forgives what it cannot pay and neither settles
    the old debt nor touches the started allowance.  The control: a plain
    book opened afterwards catches the allowance up and owes nothing."""

    builder_id = await make_builder(clean)
    granted = await grant_courtesy(clean, builder_id, 1)
    captured = courtesy_captured(granted.occurred_at)
    await record_mark(clean.engine, builder_id)
    owed = await _debit(clean, builder_id, 2 * MC, captured=None)
    assert (owed.debited_mc, owed.debt_mc) == (MC, MC)
    assert await debt_rows(clean, builder_id) == (1, 0)
    today = civil_today()
    subscription = await make_subscription(clean, builder_id)
    start, end = today + timedelta(days=2), today + timedelta(days=32)
    cycle = await make_cycle(clean, builder_id, subscription, start=start, end=end)
    await make_monthly_grant(
        clean,
        builder_id=builder_id,
        subscription_id=subscription,
        cycle_id=cycle,
        start=start,
        end=end,
    )
    await record_mark(clean.engine, builder_id, "unmark")
    clock.now = _midday(start)
    result = await _debit(clean, builder_id, MC, captured=captured, at=clock.now)
    assert (result.debited_mc, result.debt_mc, result.forgiven_mc) == (0, 0, MC)
    assert await debt_rows(clean, builder_id) == (1, 0)  # untouched, no new one
    assert await paid_lot_moves(clean, builder_id) == []
    async with clean.sessions() as session:
        book = await CreditLedger(session).open_book(builder_id, at=clock.now)
        await session.rollback()
    assert book.open_debt_mc == 0  # the plain book's own catch-up pays it


async def _claimed_trace(harness: _Harness, tenant_id: UUID, builder_id: UUID) -> str:
    """An enforced ingress claim; returns the trace effect id it anchors
    (the orphan report's decision boundary)."""

    interaction = uuid4()
    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            provider_message_id, interaction_id, payload_sha256, decision_version,
            mode
        ) VALUES (:id, :tenant, :builder, 'events', :principal, :key, NULL,
                  :interaction, :sha, 1, 'enforced')
        """,
        id=uuid4(),
        key=f"key-{interaction.hex}",
        tenant=tenant_id,
        builder=builder_id,
        principal=f"events:{tenant_id}",
        interaction=interaction,
        sha="d" * 64,
    )
    return f"trace:{tenant_id}:{interaction}:decision:1"


async def _orphans_and_alert(harness: _Harness) -> tuple[Any, bool]:
    async with harness.sessions() as session:
        service = CreditRolloutService(session)
        report = await service.orphan_report()
        readiness = await service.capability_ready()
        await session.rollback()
    return report, readiness.has_alert


async def test_forgiven_rule_traces_are_never_partial_debits(clean: _Harness) -> None:
    """A trace the rule forgives in part or in whole has ``debit + debt`` short
    of one trace for good: it is not an orphan, the readiness raises no
    alert.  The controls: a mark-era trace (courtesy plus debt) is complete,
    and a trace of a courtesy builder debited on a ``topup`` lot with no
    debt still counts."""

    _report, baseline_alert = await _orphans_and_alert(clean)
    rule = await make_builder(clean)
    tenant = await make_tenant(clean, rule)
    granted = await grant_courtesy(clean, rule, 1)
    captured = courtesy_captured(granted.occurred_at)
    _asset, reserved = await _reserve(
        clean, tenant, 2_000_000, captured, granted.occurred_at
    )
    assert reserved.charged_mc == 100  # 900 of courtesy left
    partly = await _claimed_trace(clean, tenant, rule)
    paid = await _debit(clean, rule, MC, captured=captured, effect_id=partly)
    assert (paid.debited_mc, paid.debt_mc, paid.forgiven_mc) == (900, 0, 100)
    wholly = await _claimed_trace(clean, tenant, rule)
    nothing = await _debit(clean, rule, MC, captured=captured, effect_id=wholly)
    assert (nothing.debited_mc, nothing.debt_mc, nothing.forgiven_mc) == (0, 0, MC)

    marked = await make_builder(clean)
    marked_tenant = await make_tenant(clean, marked)
    courtesy = await grant_courtesy(clean, marked, 1)
    _asset, reserved = await _reserve(
        clean,
        marked_tenant,
        2_000_000,
        courtesy_captured(courtesy.occurred_at),
        courtesy.occurred_at,
    )
    assert reserved.charged_mc == 100
    await record_mark(clean.engine, marked)
    mark_era = await _claimed_trace(clean, marked_tenant, marked)
    owed = await _debit(clean, marked, MC, captured=None, effect_id=mark_era)
    assert (owed.debited_mc, owed.debt_mc) == (900, 100)

    report, alert = await _orphans_and_alert(clean)
    assert report.trace_partial_debit == 0 and report.open_count == 0
    assert alert == baseline_alert

    # LOW-A (Group 1 re-review, added by Group 2): two controls of a builder
    # WITH courtesy that the forgiven shape must never swallow, each measured
    # alone -- one partial debit and the alert -- then removed: (a) a
    # courtesy debit short of a trace WITH debt (the rule never writes debt:
    # the exclusion's ``debt = 0``), (b) more than one trace debited on
    # courtesy lots, no debt (the rule never pays more than a trace: its
    # ``debit < TRACE_COST_MC``).
    async def courtesy_trace(
        debits: tuple[int, ...], debt_mc: int
    ) -> tuple[UUID, Any, bool]:
        builder = await make_builder(clean)
        tenant = await make_tenant(clean, builder)
        lot = (await grant_courtesy(clean, builder, 2)).lot_id
        effect = await _claimed_trace(clean, tenant, builder)
        await execute(
            clean,
            "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, "
            "effect_kind) VALUES (:id, :b, :t, :e, 'trace')",
            id=uuid4(),
            b=builder,
            t=tenant,
            e=effect,
        )
        for amount in debits:
            await execute(
                clean,
                "INSERT INTO credit_ledger_entries (id, builder_id, account_id, "
                "effect_id, lot_id, entry_type, amount_mc, occurred_at) SELECT "
                ":id, :b, a.id, :e, :lot, 'debit', :amount, now() FROM "
                "credit_accounts a WHERE a.builder_id = :b",
                id=uuid4(),
                b=builder,
                e=effect,
                lot=lot,
                amount=amount,
            )
        if debt_mc:
            await execute(
                clean,
                "INSERT INTO credit_debts (id, builder_id, account_id, "
                "debt_source_kind, debt_source_effect_id, amount_mc) SELECT :id, "
                ":b, a.id, 'consumption', :e, :amount FROM credit_accounts a "
                "WHERE a.builder_id = :b",
                id=uuid4(),
                b=builder,
                e=effect,
                amount=debt_mc,
            )
        measured, measured_alert = await _orphans_and_alert(clean)
        for sql in (
            "DELETE FROM credit_debts WHERE builder_id = :b",
            "DELETE FROM credit_ledger_entries WHERE builder_id = :b "
            "AND effect_id = :e",
            "DELETE FROM credit_effects WHERE builder_id = :b AND effect_id = :e",
            "DELETE FROM credit_ingress_claims WHERE builder_id = :b",
        ):
            await plant(clean, sql, b=builder, e=effect)
        return builder, measured, measured_alert

    for debits, debt_mc in (((400,), 300), ((1_000, 500), 0)):
        _builder, measured, measured_alert = await courtesy_trace(debits, debt_mc)
        assert measured.trace_partial_debit == 1, (debits, debt_mc)
        assert measured_alert, (debits, debt_mc)
    cleared, cleared_alert = await _orphans_and_alert(clean)
    assert cleared.trace_partial_debit == 0 and cleared.open_count == 0
    assert cleared_alert == baseline_alert

    control = await make_builder(clean)
    control_tenant = await make_tenant(clean, control)
    await grant_courtesy(clean, control, 1)
    order = await make_topup(clean, control)
    topup_lot = await scalar(
        clean,
        "SELECT id FROM credit_lots WHERE builder_id = :b AND order_id = :o",
        b=control,
        o=order,
    )
    planted = await _claimed_trace(clean, control_tenant, control)
    await execute(
        clean,
        "INSERT INTO credit_effects (id, builder_id, tenant_id, effect_id, "
        "effect_kind) VALUES (:id, :b, :t, :e, 'trace')",
        id=uuid4(),
        b=control,
        t=control_tenant,
        e=planted,
    )
    await execute(
        clean,
        "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
        "lot_id, entry_type, amount_mc, occurred_at) SELECT :id, :b, a.id, :e, "
        ":lot, 'debit', 500, now() FROM credit_accounts a WHERE a.builder_id = :b",
        id=uuid4(),
        b=control,
        e=planted,
        lot=topup_lot,
    )
    controlled, controlled_alert = await _orphans_and_alert(clean)
    assert controlled.trace_partial_debit == 1 and controlled.open_count == 1
    assert controlled_alert  # the control trips the alert the rule never does


# ---------------------------------------------------------------------------
# Windows and storage (wish Decision 6).
# ---------------------------------------------------------------------------


def _document(tenant: UUID, characters: int) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant,
        title="Doc",
        content="x" * characters,
        content_type="text",
        document_metadata={},
        is_indexed=False,
        chunk_count=0,
        size_bytes=characters,
    )


def _media(tenant: UUID, size: int) -> MediaAsset:
    payload = b"%PDF-" + b"0" * (size - 5)
    return MediaAsset(
        id=uuid4(),
        tenant_id=tenant,
        display_name="Media",
        original_filename="media.pdf",
        category="pdf",
        mime_type="application/pdf",
        size_bytes=size,
        sha256=media_payload_sha256(payload),
        payload=payload,
        usage_trigger=None,
        auto_suggest=False,
    )


async def _store(harness: _Harness, *rows: Any) -> list[UUID]:
    async with harness.sessions() as session, session.begin():
        session.add_all(rows)
    return [row.id for row in rows]


async def _reserve(
    harness: _Harness,
    tenant: UUID,
    size: int,
    captured: EffectiveEnforcement | None,
    enforcement_at: datetime,
) -> tuple[UUID, credit_inventory.AllocationOutcome]:
    asset = _media(tenant, size)
    async with harness.sessions() as session, session.begin():
        session.add(asset)
        await session.flush()
        outcome = await credit_inventory.reserve_upload(
            session,
            tenant_id=tenant,
            category="media",
            object_ids=[asset.id],
            upload_id=asset.id,
            enforcement_at=enforcement_at,
            captured=captured,
        )
        if outcome.status == "credit_pending":
            await session.rollback()
    return asset.id, outcome


def _recheck_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """debito-cortesia-automatico Group 2: ``_enforced_book`` re-checks a
    captured courtesy mode under the account lock by ``(origin, at, state)``
    (the race, proven in
    tests/integration/test_credit_effective_enforcement_postgres.py).  The
    two storage proofs below hold ONE capture fixed across the operator's
    grants and removals -- and simulated months -- to exercise the
    allocation arithmetic alone, so the re-check is a no-op there."""

    async def elsewhere(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(credit_inventory, "require_captured_enforcement", elsewhere)


def _next_months(today: date) -> tuple[datetime, datetime]:
    """Two instants in the two calendar windows after the real one."""

    first = add_calendar_months(today.replace(day=1), 1)
    return _midday(first + timedelta(days=9)), _midday(
        add_calendar_months(first, 1) + timedelta(days=4)
    )


async def test_the_first_grant_window_is_free_and_later_ones_charge_courtesy_only(
    clean: _Harness, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _recheck_elsewhere(monkeypatch)
    builder_id = await make_builder(clean)
    tenant = await make_tenant(clean, builder_id)
    await _store(clean, _document(tenant, 50_000), _media(tenant, 30_000_000))
    await make_topup(clean, builder_id)  # a paid lot the rule never touches
    month_one, month_two = _next_months(civil_today())
    clock.now = month_one
    granted = await grant_courtesy(clean, builder_id, 12, at=month_one)
    captured = courtesy_captured(granted.occurred_at)

    # The window of ``at``: a free baseline per category; the trace pays 1.
    first = await _debit(clean, builder_id, MC, captured=captured, at=month_one)
    assert first.debited_mc == MC
    openings = [
        (a.window_start, a.category, a.allocation_kind, a.target_mc, a.charged_mc)
        for a in await _allocations(clean, builder_id)
    ]
    window_one = civil_date(month_one).replace(day=1)
    assert sorted(openings) == [
        (window_one, "media", "baseline", 1_500, 0),
        (window_one, "rag", "baseline", 10_000, 0),
    ]

    # The next window: maintenance from courtesy only; 11 credits left pay
    # the RAG (10) and 1 of the media's 1.5 -- the rest is no debt and stays
    # outside the coverage; the trace then finds nothing and is forgiven.
    clock.now = month_two
    trace = f"trace:{uuid4()}:decision:1"
    second = await _debit(
        clean, builder_id, MC, captured=captured, at=month_two, effect_id=trace
    )
    assert (second.debited_mc, second.debt_mc, second.forgiven_mc) == (0, 0, MC)
    window_two = civil_date(month_two).replace(day=1)
    maintenance = {
        a.category: (a.allocation_kind, a.target_mc, a.charged_mc, a.effect_id)
        for a in await _allocations(clean, builder_id)
        if a.window_start == window_two
    }
    assert maintenance == {
        "rag": ("maintenance", 10_000, 10_000, f"window:{window_two}:rag-maintenance"),
        "media": (
            "maintenance",
            1_000,
            1_000,
            f"window:{window_two}:media-maintenance",
        ),
    }
    assert await _entries(clean, builder_id, trace) == []

    # A new courtesy, then a media upload: the reservation bills the gap the
    # maintenance left (500) plus its own share (100).
    clock.now = month_two + timedelta(hours=1)
    regrant = await grant_courtesy(clean, builder_id, 5, at=clock.now)
    asset, reserved = await _reserve(
        clean, tenant, 2_000_000, captured, granted.occurred_at
    )
    assert (reserved.status, reserved.charged_mc, reserved.target_mc) == (
        "applied",
        600,
        1_600,
    )
    effect = f"media:{tenant}:{asset}:reserve"
    assert await _entries(clean, builder_id, effect) == [("debit", regrant.lot_id, 600)]

    # Nothing left to pay with: the reservation is recorded with charged 0
    # and no effect, the coverage does not move (wish Decision 6).
    await remove_courtesy(clean, regrant.lot_id, at=clock.now)
    asset, unpaid = await _reserve(
        clean, tenant, 2_000_000, captured, granted.occurred_at
    )
    assert (unpaid.status, unpaid.charged_mc, unpaid.target_mc) == ("applied", 0, 1_700)
    effect = f"media:{tenant}:{asset}:reserve"
    [row] = [a for a in await _allocations(clean, builder_id) if a.source_key == effect]
    assert (row.allocation_kind, row.target_mc, row.charged_mc, row.effect_id) == (
        "reservation",
        1_600,
        0,
        None,
    )
    assert not await _effect_exists(clean, builder_id, effect)
    assert await debt_rows(clean, builder_id) == (0, 0)
    assert await paid_lot_moves(clean, builder_id) == []
    assert await _lifecycle_rows(clean, builder_id) == (0, 0)


async def test_egress_above_the_included_amount_is_paid_by_courtesy_only(
    clean: _Harness, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _recheck_elsewhere(monkeypatch)
    builder_id = await make_builder(clean)
    tenant = await make_tenant(clean, builder_id)
    [media] = await _store(clean, _media(tenant, 1_000_000))
    await make_topup(clean, builder_id)  # a paid lot the rule never touches
    month_one, _later = _next_months(civil_today())
    clock.now = month_one
    granted = await grant_courtesy(clean, builder_id, 1, at=month_one)
    captured = courtesy_captured(granted.occurred_at)
    await remove_courtesy(clean, granted.lot_id, at=month_one)

    async def download(size: int) -> credit_inventory.AllocationOutcome:
        async with clean.sessions() as session, session.begin():
            return await credit_inventory.charge_egress(
                session,
                tenant_id=tenant,
                media_id=media,
                size_bytes=size,
                enforcement_at=granted.occurred_at,
                captured=captured,
            )

    # The window opens with a free media baseline of 50 mc (1 MB stored),
    # which includes 10 MB of downloads.  400 MB with no courtesy left: the
    # overage target (390 MB) is recorded unpaid, outside the coverage.
    unpaid = await download(400_000_000)
    assert (unpaid.status, unpaid.charged_mc, unpaid.target_mc) == ("applied", 0, 3_900)
    clock.now = month_one + timedelta(hours=1)
    regrant = await grant_courtesy(clean, builder_id, 5, at=clock.now)
    paid = await download(10_000_000)
    # The gap (3.9) plus this download's 0.1, paid by courtesy alone.
    assert (paid.charged_mc, paid.target_mc) == (4_000, 4_000)
    egress = sorted(
        (a.target_mc, a.charged_mc, a.effect_id is None)
        for a in await _allocations(clean, builder_id)
        if a.category == "egress"
    )
    assert egress == [(0, 0, True), (4_000, 4_000, False)]
    async with clean.engine.connect() as connection:
        lots = {
            str(lot)
            for (lot,) in await connection.execute(
                text(
                    "SELECT DISTINCT e.lot_id FROM credit_ledger_entries e JOIN "
                    "credit_effects f ON f.builder_id = e.builder_id AND "
                    "f.effect_id = e.effect_id WHERE e.builder_id = :b AND "
                    "f.effect_kind = 'egress'"
                ),
                {"b": builder_id},
            )
        }
    assert lots == {str(regrant.lot_id)}
    assert await debt_rows(clean, builder_id) == (0, 0)
    assert await paid_lot_moves(clean, builder_id) == []


async def test_under_the_rule_a_reservation_skips_the_binding_and_the_hold(
    clean: _Harness,
) -> None:
    """DESIGN item 7: an unbound current cycle (a reconciliation failure)
    and a quarantine refuse the plain reservation; under the rule the same
    reservation is paid by courtesy."""

    builder_id = await make_builder(clean)
    tenant = await make_tenant(clean, builder_id)
    subscription = await make_subscription(clean, builder_id)
    today = civil_today()
    await make_cycle(
        clean,
        builder_id,
        subscription,
        start=today - timedelta(days=3),
        end=today + timedelta(days=27),
        state="quarantined",
    )
    # A paid lot the rule never touches, expiring BEFORE the courtesy: plain
    # FEFO would spend it first.
    await make_topup(
        clean, builder_id, expires_at=datetime.now(timezone.utc) + timedelta(days=3)
    )
    granted = await grant_courtesy(clean, builder_id, 10)
    async with clean.sessions() as session:
        coverage = await CreditRolloutService(session).binding_coverage(builder_id)
        await session.rollback()
    assert not coverage.usable
    _asset, refused = await _reserve(
        clean, tenant, 2_000_000, None, granted.occurred_at
    )
    assert refused.status == "credit_pending"
    captured = courtesy_captured(granted.occurred_at)
    asset, reserved = await _reserve(
        clean, tenant, 2_000_000, captured, granted.occurred_at
    )
    assert (reserved.status, reserved.charged_mc) == ("applied", 100)
    assert await _entries(clean, builder_id, f"media:{tenant}:{asset}:reserve") == [
        ("debit", granted.lot_id, 100)
    ]
    assert await paid_lot_moves(clean, builder_id) == []


@pytest.mark.parametrize("origin", ["mark", "global"])
async def test_a_mark_or_global_captured_reservation_keeps_the_coverage_gate(
    clean: _Harness, origin: str
) -> None:
    """Only a COURTESY capture skips the binding-coverage gate
    (``_enforced_book``): a reservation captured under a mark or the global,
    for a builder that also holds courtesy, still refuses an unusable
    coverage (an unbound current cycle, NOT quarantined, so the ledger's
    quarantine hold is not what refuses) with ``credit_pending`` and writes
    nothing."""

    builder_id = await make_builder(clean)
    tenant = await make_tenant(clean, builder_id)
    subscription = await make_subscription(clean, builder_id)
    today = civil_today()
    # An unbound current cycle outside its grace, NOT quarantined: the
    # coverage is unusable and the ledger's quarantine hold is not active.
    await make_cycle(
        clean,
        builder_id,
        subscription,
        start=today - timedelta(days=10),
        end=today + timedelta(days=20),
    )
    granted = await grant_courtesy(clean, builder_id, 10)
    if origin == "mark":
        await record_mark(clean.engine, builder_id)
    else:
        at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await execute(
            clean,
            "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, "
            "set_by, set_at) VALUES (1, :at, 'test', :at)",
            at=at,
        )
    async with clean.sessions() as session:
        repository = CreditRepository(session)
        coverage = await CreditRolloutService(session).binding_coverage(builder_id)
        quarantined = await repository.quarantine_hold(builder_id)
        effective = await repository.effective_enforcement(builder_id)
        await session.rollback()
    assert not coverage.usable and not quarantined
    assert effective.origin == origin
    before = await _credit_writes(clean, builder_id, tenant)
    captured = EffectiveEnforcement(effective.at, origin, builder_id=builder_id)
    _asset, outcome = await _reserve(clean, tenant, 2_000_000, captured, effective.at)
    assert outcome.status == "credit_pending"
    assert await _credit_writes(clean, builder_id, tenant) == before
    assert granted.lot_id is not None


async def _credit_writes(
    harness: _Harness, builder_id: UUID, tenant: UUID
) -> tuple[int, ...]:
    """Every row a reservation or an egress charge could write."""

    counts = []
    for table in (
        "credit_allocations",
        "credit_effects",
        "credit_ledger_entries",
        "credit_debts",
    ):
        counts.append(
            int(
                await scalar(
                    harness,
                    f"SELECT count(*) FROM {table} WHERE builder_id = :b",
                    b=builder_id,
                )
            )
        )
    counts.append(
        int(
            await scalar(
                harness,
                "SELECT count(*) FROM media_assets WHERE tenant_id = :t",
                t=tenant,
            )
        )
    )
    return tuple(counts)


async def test_a_stale_courtesy_capture_is_refused_under_the_account_lock(
    clean: _Harness,
) -> None:
    """MEDIUM-1 (DESIGN item 11, Decision 15): ``_enforced_book`` re-checks
    a captured courtesy mode by ``(origin, at, state)`` under the account
    lock.  ``charged`` captured, then every courtesy removed with no paid
    plan (now ``blocked``): ``reserve_upload`` and ``charge_egress`` with the
    stale capture raise ``CreditEnforcementChanged`` and write nothing; the
    same for a ``blocked`` capture followed by a new grant (now
    ``charged``)."""

    builder_id = await make_builder(clean)
    tenant = await make_tenant(clean, builder_id)
    [media] = await _store(clean, _media(tenant, 1_000_000))
    granted = await grant_courtesy(clean, builder_id, 5)
    charged = courtesy_captured(granted.occurred_at)
    assert await effective_of(clean, builder_id) == charged

    async def reserve(captured: EffectiveEnforcement) -> None:
        await _reserve(clean, tenant, 2_000_000, captured, granted.occurred_at)

    async def egress(captured: EffectiveEnforcement) -> None:
        async with clean.sessions() as session, session.begin():
            await credit_inventory.charge_egress(
                session,
                tenant_id=tenant,
                media_id=media,
                size_bytes=400_000_000,
                enforcement_at=granted.occurred_at,
                captured=captured,
            )

    await remove_courtesy(clean, granted.lot_id)
    blocked = _blocked(granted.occurred_at)
    assert await effective_of(clean, builder_id) == blocked
    before = await _credit_writes(clean, builder_id, tenant)
    for attempt in (reserve, egress):
        with pytest.raises(CreditEnforcementChanged):
            await attempt(charged)
    assert await _credit_writes(clean, builder_id, tenant) == before

    await grant_courtesy(clean, builder_id, 5)
    assert await effective_of(clean, builder_id) == charged
    before = await _credit_writes(clean, builder_id, tenant)
    for attempt in (reserve, egress):
        with pytest.raises(CreditEnforcementChanged):
            await attempt(blocked)
    assert await _credit_writes(clean, builder_id, tenant) == before


# ---------------------------------------------------------------------------
# The lifecycle under the rule (DESIGN item 4).
# ---------------------------------------------------------------------------


async def test_no_episode_and_thresholds_on_the_courtesy_basis(clean: _Harness) -> None:
    builder_id = await make_builder(clean)
    first = await grant_courtesy(clean, builder_id, 10)
    captured = courtesy_captured(first.occurred_at)
    key = f"c:courtesy:{first.event_id}:grant"
    for amount in (5 * MC, 4 * MC, MC, MC):
        await _debit(clean, builder_id, amount, captured=captured)
    assert await _alerts(clean, builder_id) == [
        (key, 50, 10_000, 5_000),
        (key, 90, 10_000, 1_000),
        (key, 100, 10_000, 0),
    ]
    assert await _lifecycle_rows(clean, builder_id) == (0, 0)
    # A new grant re-arms on its own key; the courtesy in force is 14.
    second = await grant_courtesy(clean, builder_id, 4)
    rearmed = f"c:courtesy:{second.event_id}:grant"
    assert (await _alerts(clean, builder_id))[3:] == [(rearmed, 50, 14_000, 4_000)]
    assert len(rearmed) <= 120
    assert await _lifecycle_rows(clean, builder_id) == (0, 0)
    assert await debt_rows(clean, builder_id) == (0, 0)


async def test_a_removal_is_never_usage_for_the_courtesy_thresholds(
    clean: _Harness,
) -> None:
    builder_id = await make_builder(clean)
    granted = await grant_courtesy(clean, builder_id, 10)
    await remove_courtesy(clean, granted.lot_id, credits=6)  # 4 in force, 0 used
    assert await _alerts(clean, builder_id) == []
    await remove_courtesy(clean, granted.lot_id)  # "Tirar tudo": nothing in force
    assert await _alerts(clean, builder_id) == []
    assert await effective_of(clean, builder_id) == _blocked(granted.occurred_at)
    assert await _lifecycle_rows(clean, builder_id) == (0, 0)


async def test_the_request_gate_and_the_worker_are_inert_under_the_rule(
    clean: _Harness, clock: _Clock
) -> None:
    """An episode left open from a mark era (exhausted while marked, then
    unmarked and given courtesy): under the rule, past its deadline, the
    gate answers ``ok`` and writes nothing -- no transition, no settlement
    -- and the worker selects nobody; once marked again it acts."""

    start = clock.now
    builder_id = await make_builder(clean)
    await record_mark(clean.engine, builder_id, occurred_at=start)
    clock.now = start + timedelta(hours=1)
    owed = await _debit(clean, builder_id, MC, captured=None, at=clock.now)
    assert owed.debt_mc == MC
    episodes, _transitions = await _lifecycle_rows(clean, builder_id)
    assert episodes == 1
    await record_mark(
        clean.engine, builder_id, "unmark", occurred_at=start + timedelta(hours=2)
    )
    clock.now = start + timedelta(hours=3)
    granted = await grant_courtesy(clean, builder_id, 5, at=clock.now)
    assert await effective_of(clean, builder_id) == _charged(granted.occurred_at)
    before = await _lifecycle_rows(clean, builder_id)
    entries = await scalar(
        clean,
        "SELECT count(*) FROM credit_ledger_entries WHERE builder_id = :b",
        b=builder_id,
    )

    clock.now = start + timedelta(hours=100)  # past the 72 h deadline
    async with clean.sessions() as session:
        with _statements(clean.engine) as seen:
            verdict = await credit_lifecycle.request_gate(
                session,
                builder_id,
                now=clock.now,
                captured=courtesy_captured(granted.occurred_at),
            )
        assert verdict == "ok" and seen == []
        await session.rollback()
    async with clean.sessions() as session, session.begin():
        assert (
            await credit_lifecycle.request_gate(session, builder_id, now=clock.now)
            == "ok"
        )
    assert await _lifecycle_rows(clean, builder_id) == before
    assert (
        await scalar(
            clean,
            "SELECT count(*) FROM credit_ledger_entries WHERE builder_id = :b",
            b=builder_id,
        )
        == entries
    )
    assert await credit_lifecycle.run_lifecycle_once(clean.sessions, now=clock.now) == 0
    assert await _lifecycle_rows(clean, builder_id) == before

    # The control: marked again, the same gate evaluates and writes.
    await record_mark(clean.engine, builder_id, occurred_at=clock.now)
    async with clean.sessions() as session, session.begin():
        await credit_lifecycle.request_gate(session, builder_id, now=clock.now)
    assert await _lifecycle_rows(clean, builder_id) != before
