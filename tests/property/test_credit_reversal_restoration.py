"""Cumulative reversal and restoration of one credit-bearing line (Group 2).

A line with prior consumption (paid partly by other lots) receives an
arbitrary monotone sequence of cumulative snapshots -- partial refunds,
chargebacks, wins -- delivered out of order and with replays.  For every
sequence:

* each restoration effect splits its delta exactly: ``debt_release +
  settlement_release + lot_regrant = restoration_delta``;
* each reversal effect withdraws or owes exactly its delta (the lot is not
  expired here, so nothing is absorbed);
* no restoration exceeds the reversal; delivering everything again writes
  nothing (exactly once);
* the result does not depend on the delivery order: it equals applying only
  the last snapshot;
* conservation closes per line (``usable + consumed - reversal debt in force
  = granted - net reversed``) and per account.

Through expiry (lots running out between deliveries) the split and the
exactly-once properties still hold, a reversal moves its whole delta unless
the line had expired (the rest is absorbed, owner decision O1), expired lots
hold nothing, and open debt never exceeds consumption.  The differential
property pins time exactly: a chargeback won at once leaves any history --
grants, debits, time, other lines' reversals -- where it would have been
without it.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from hypothesis import event, given, settings
from hypothesis import strategies as st

from src.repositories.credit import LedgerMovement, fold_ledger
from src.services.credit_ledger import (
    GrantSpec,
    LedgerBook,
    LotState,
    cumulative_target_mc,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    parse_line_effect_id,
    topup_grant_effect_id,
    topup_lot_expires_at,
)

EXAMPLES = int(os.environ.get("CREDIT_PROPERTY_EXAMPLES", "300"))
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
SETTLED = 99_900
SUBSCRIPTION = UUID("5a000000-0000-4000-8000-00000000000b")


@st.composite
def _snapshots(draw: st.DrawFn) -> list[tuple[int, int]]:
    """A monotone cumulative sequence ``(reversed_cents, restored_cents)``."""

    steps = draw(st.integers(1, 6))
    reversed_cents = restored = 0
    sequence = []
    for _ in range(steps):
        reversed_cents = draw(st.integers(reversed_cents, SETTLED))
        restored = draw(st.integers(restored, reversed_cents))
        sequence.append((reversed_cents, restored))
    if draw(st.booleans()):
        sequence.append((SETTLED, SETTLED))  # a chargeback, then won
    return sequence


SCENARIOS = st.fixed_dictionaries(
    {
        "monthly": st.booleans(),
        "grant": st.integers(1, 40).map(lambda credits: credits * 1_000),
        "others": st.lists(st.integers(1, 40).map(lambda c: c * 1_000), max_size=3),
        "consumed_share": st.integers(0, 100),
        "snapshots": _snapshots(),
        "order_seed": st.randoms(use_true_random=False),
        "replays": st.integers(0, 4),
    }
)


def _book(monthly: bool, grant: int, others: list[int]) -> tuple[LedgerBook, UUID]:
    book = LedgerBook(builder_id=uuid4(), account_id=uuid4())
    if monthly:
        start, end = date(2026, 10, 12), date(2026, 11, 12)
        line = book.grant(
            GrantSpec(
                lot_kind="monthly",
                effect_id=monthly_grant_effect_id(SUBSCRIPTION, start, end),
                payment_id="pay-line",
                paid_at=start,
                granted_mc=grant,
                expires_at=monthly_lot_expires_at(end),
                subscription_id=SUBSCRIPTION,
                cycle_start=start,
                cycle_end=end,
            ),
            at=NOW,
        )
    else:
        order_id = UUID(int=1)
        line = book.grant(
            GrantSpec(
                lot_kind="topup",
                effect_id=topup_grant_effect_id(order_id),
                payment_id="pay-line",
                paid_at=date(2026, 10, 1),
                granted_mc=grant,
                expires_at=topup_lot_expires_at(date(2026, 10, 1)),
                order_id=order_id,
            ),
            at=NOW,
        )
    for index, amount in enumerate(others):
        order_id = UUID(int=100 + index)
        paid_at = date(2026, 9, 1) + timedelta(days=index * 7)
        book.grant(
            GrantSpec(
                lot_kind="topup",
                effect_id=topup_grant_effect_id(order_id),
                payment_id=f"pay-{index}",
                paid_at=paid_at,
                granted_mc=amount,
                expires_at=topup_lot_expires_at(paid_at),
                order_id=order_id,
            ),
            at=NOW,
        )
    return book, line.id


def _deliver(
    book: LedgerBook, lot_id: UUID, revision: int, snapshot: tuple[int, int], grant: int
) -> None:
    reversed_cents, restored = snapshot
    book.apply_line_targets(
        lot_id,
        line_id="plan",
        payment_id="pay-line",
        financial_revision=revision,
        reversal_target_mc=cumulative_target_mc(grant, reversed_cents, SETTLED),
        restoration_target_mc=cumulative_target_mc(grant, restored, SETTLED),
        at=NOW + timedelta(minutes=revision),
    )


def _trigger(book: LedgerBook) -> dict[str, str]:
    """``return:`` effect -> the restoration or release that set it off."""

    by_digest = {
        hashlib.sha256(effect_id.encode("utf-8")).hexdigest(): effect_id
        for effect_id in book.writes.effect_ids
    }
    return {
        effect_id: by_digest[effect_id.rpartition(":")[2]]
        for effect_id in book.writes.effect_ids
        if effect_id.startswith("return:")
    }


def _effect_amounts(book: LedgerBook) -> dict[str, Counter[str]]:
    """Entry totals per effect, each ``return:`` effect (one released debt)
    counted with the restoration or release that set it off."""

    trigger = _trigger(book)
    amounts: dict[str, Counter[str]] = {}
    for entry in book.writes.entries:
        effect_id = str(entry["effect_id"])
        amounts.setdefault(trigger.get(effect_id, effect_id), Counter())[
            str(entry["entry_type"])
        ] += int(entry["amount_mc"])
    return amounts


def _line_debt_in_force(book: LedgerBook, lot_id: UUID) -> int:
    """From the rows alone: this line's reversal debt, net of releases."""

    owed = sum(
        int(row["amount_mc"])
        for row in book.writes.debts
        if row["original_grant_lot_id"] == lot_id
    )
    debts_of_line = {
        str(row["id"])
        for row in book.writes.debts
        if row["original_grant_lot_id"] == lot_id
    }
    released = sum(
        int(entry["amount_mc"])
        for entry in book.writes.entries
        if entry["entry_type"] in ("debt_release", "settlement_release")
        and str(entry["effect_id"]).split(":")[1] in debts_of_line
    )
    return owed - released


@settings(max_examples=400, deadline=None)
@given(SCENARIOS)
def test_any_cumulative_sequence_is_split_exactly_once(scenario: dict) -> None:  # type: ignore[type-arg]
    grant: int = scenario["grant"]
    book, lot_id = _book(scenario["monthly"], grant, scenario["others"])
    total = grant + sum(scenario["others"])
    consumed = total * scenario["consumed_share"] // 100
    if consumed:
        book.debit("trace:prior", consumed, at=NOW, effect_kind="trace")

    snapshots: list[tuple[int, int]] = scenario["snapshots"]
    deliveries = list(enumerate(snapshots, start=2))
    deliveries += [deliveries[i % len(deliveries)] for i in range(scenario["replays"])]
    scenario["order_seed"].shuffle(deliveries)
    applied_revision = 0
    for revision, snapshot in deliveries:
        if revision <= applied_revision:
            continue  # the consumer: replay or older revision is a no-op
        applied_revision = revision
        _deliver(book, lot_id, revision, snapshot, grant)
        assert book.applied_restoration_mc(lot_id) <= book.applied_reversal_mc(lot_id)

    # Every effect moved exactly its cumulative delta.
    previous = {"reversal": 0, "restoration": 0}
    parsed = sorted(
        (parse_line_effect_id(effect_id), effect_id)
        for effect_id in book.writes.effect_ids
        if parse_line_effect_id(effect_id) is not None
    )
    amounts = _effect_amounts(book)
    debts_by_effect = Counter(
        {row["debt_source_effect_id"]: row["amount_mc"] for row in book.writes.debts}
    )
    for parsed_id, effect_id in parsed:
        assert parsed_id is not None
        kind, _lot, target = parsed_id
        delta = target - previous[kind]
        previous[kind] = target
        moved = amounts.get(effect_id, Counter())
        if kind == "restoration":
            assert (
                moved["debt_release"]
                + moved["settlement_release"]
                + moved["lot_regrant"]
                == delta
            )
            for entry_type in ("debt_release", "settlement_release", "lot_regrant"):
                if moved[entry_type]:
                    event(f"restoration wrote {entry_type}")
        else:
            assert moved["reversal"] + debts_by_effect[effect_id] == delta

    # Exactly once: delivering everything again writes nothing.
    written = len(book.writes.entries)
    for revision, snapshot in deliveries:
        _deliver(book, lot_id, revision, snapshot, grant)
    assert len(book.writes.entries) == written

    # Order independence: the last snapshot alone gives the same result.
    direct, direct_lot = _book(scenario["monthly"], grant, scenario["others"])
    if consumed:
        direct.debit("trace:prior", consumed, at=NOW, effect_kind="trace")
    _deliver(direct, direct_lot, len(snapshots) + 1, snapshots[-1], grant)
    by_kind = {lot.order_id or lot.lot_kind: lot.balance_mc for lot in book.lots}
    assert by_kind == {
        lot.order_id or lot.lot_kind: lot.balance_mc for lot in direct.lots
    }
    assert book.open_debt_mc == direct.open_debt_mc

    # Conservation per line and per account (nothing expired).
    line: LotState = book.lot(lot_id)
    reversed_cents, restored = snapshots[-1]
    net = cumulative_target_mc(grant, reversed_cents, SETTLED) - cumulative_target_mc(
        grant, restored, SETTLED
    )
    assert (
        line.available_mc(NOW) + line.consumed_mc - _line_debt_in_force(book, lot_id)
        == grant - net
    )
    assert book.available_mc(NOW) - book.open_debt_mc == total - consumed - net


JUMPS = st.sampled_from(
    [
        None,
        datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc),  # the monthly ended
        datetime(2027, 5, 1, 12, 0, tzinfo=timezone.utc),  # every lot ended
    ]
)


@settings(max_examples=EXAMPLES, deadline=None)
@given(SCENARIOS, JUMPS, st.integers(0, 8))
def test_cumulative_sequences_through_expiry_keep_the_split(
    scenario: dict, jump: datetime | None, jump_index: int  # type: ignore[type-arg]
) -> None:
    grant: int = scenario["grant"]
    book, lot_id = _book(scenario["monthly"], grant, scenario["others"])
    total = grant + sum(scenario["others"])
    consumed = total * scenario["consumed_share"] // 100
    if consumed:
        book.debit("trace:prior", consumed, at=NOW, effect_kind="trace")
    snapshots: list[tuple[int, int]] = scenario["snapshots"]
    deliveries = list(enumerate(snapshots, start=2))
    deliveries += [deliveries[i % len(deliveries)] for i in range(scenario["replays"])]
    scenario["order_seed"].shuffle(deliveries)

    created_at: dict[str, datetime] = {}
    applied_revision = 0
    now = NOW
    for index, (revision, snapshot) in enumerate(deliveries):
        if jump is not None and index == jump_index:
            now = jump
            book.sweep_expired(now)
        now += timedelta(minutes=1)
        if revision <= applied_revision:
            continue
        applied_revision = revision
        known = set(book.writes.effect_ids)
        reversed_cents, restored = snapshot
        book.apply_line_targets(
            lot_id,
            line_id="plan",
            payment_id="pay-line",
            financial_revision=revision,
            reversal_target_mc=cumulative_target_mc(grant, reversed_cents, SETTLED),
            restoration_target_mc=cumulative_target_mc(grant, restored, SETTLED),
            at=now,
        )
        for effect_id in set(book.writes.effect_ids) - known:
            created_at[effect_id] = now
        assert book.applied_restoration_mc(lot_id) <= book.applied_reversal_mc(lot_id)

    previous = {"reversal": 0, "restoration": 0}
    amounts = _effect_amounts(book)
    debts_by_effect = Counter(
        {row["debt_source_effect_id"]: row["amount_mc"] for row in book.writes.debts}
    )
    line = book.lot(lot_id)
    for parsed_id, effect_id in sorted(
        (parse_line_effect_id(effect_id), effect_id)
        for effect_id in book.writes.effect_ids
        if parse_line_effect_id(effect_id) is not None
    ):
        assert parsed_id is not None
        kind, _lot, target = parsed_id
        delta, previous[kind] = target - previous[kind], target
        moved = amounts.get(effect_id, Counter())
        if kind == "restoration":
            assert (
                moved["debt_release"]
                + moved["settlement_release"]
                + moved["lot_regrant"]
                == delta
            )
            if moved["expiry"]:
                event("restoration into an expired lot")
        else:
            shifted = moved["reversal"] + debts_by_effect[effect_id]
            assert shifted <= delta
            if not line.expired(created_at[effect_id]):
                assert shifted == delta
            elif shifted < delta:
                event("reversal absorbed by expiry")

    written = len(book.writes.entries)
    for revision, snapshot in deliveries:
        _deliver(book, lot_id, revision, snapshot, grant)
    assert len(book.writes.entries) == written

    book.sweep_expired(now)
    for lot in book.lots:
        if lot.expired(now):
            assert lot.balance_mc == 0
    assert book.open_debt_mc <= consumed
    if book.open_debt_mc:
        assert book.available_mc(now) == 0
    reversed_cents, restored = snapshots[-1]
    net = cumulative_target_mc(grant, reversed_cents, SETTLED) - cumulative_target_mc(
        grant, restored, SETTLED
    )
    expired = sum(
        int(e["amount_mc"]) for e in book.writes.entries if e["entry_type"] == "expiry"
    )
    position = book.available_mc(now) - book.open_debt_mc
    assert total - consumed - net - expired <= position <= total - consumed - net


# ---------------------------------------------------------------------------
# Differential: a chargeback won at once changes nothing, whatever the time
# (adopted from the 2026-10-26 independent review).
# ---------------------------------------------------------------------------

_OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("topup"), st.integers(1, 20), st.integers(-200, 0)),
        st.tuples(st.just("debit"), st.integers(1, 30_000)),
        st.tuples(st.just("advance"), st.integers(1, 24 * 60)),
        st.tuples(
            st.just("rev"),
            st.integers(0, 30),
            st.integers(0, 5_000),
            st.integers(0, 5_000),
        ),
    ),
    max_size=30,
)
_TOPUP_SETTLED = 10_000


class _Run:
    def __init__(self) -> None:
        self.book = LedgerBook(builder_id=UUID(int=7), account_id=UUID(int=8))
        self.now = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
        self.lots: list[UUID] = []
        self.cents: dict[UUID, list[int]] = {}
        self.serial = 0
        self.grants = 0
        self.consumed = 0
        self.won: UUID | None = None

    def grant(self, credits: int, offset: int) -> None:
        self.grants += 1
        order = UUID(int=1_000 + self.grants)
        paid = date(2026, 10, 1) + timedelta(days=offset)
        lot = self.book.grant(
            GrantSpec(
                lot_kind="topup",
                effect_id=topup_grant_effect_id(order),
                payment_id=f"p{self.grants}",
                paid_at=paid,
                granted_mc=credits * 1_000,
                expires_at=topup_lot_expires_at(paid),
                order_id=order,
            ),
            at=self.now,
        )
        self.lots.append(lot.id)
        self.cents[lot.id] = [0, 0]

    def targets(self, lot_id: UUID) -> None:
        self.serial += 1
        reversed_cents, restored = self.cents[lot_id]
        grant = self.book.lot(lot_id).granted_mc
        self.book.apply_line_targets(
            lot_id,
            line_id="l",
            payment_id="p",
            financial_revision=self.serial,
            reversal_target_mc=cumulative_target_mc(
                grant, reversed_cents, _TOPUP_SETTLED
            ),
            restoration_target_mc=cumulative_target_mc(grant, restored, _TOPUP_SETTLED),
            at=self.now,
        )

    def step(self, operation: tuple[object, ...]) -> None:
        kind = operation[0]
        if kind == "topup":
            credits, offset = operation[1], operation[2]
            assert isinstance(credits, int) and isinstance(offset, int)
            self.grant(credits, offset)
        elif kind == "debit":
            amount = operation[1]
            assert isinstance(amount, int)
            self.serial += 1
            self.book.debit(
                f"trace:{self.serial}", amount, at=self.now, effect_kind="trace"
            )
            self.consumed += amount
        elif kind == "advance":
            hours = operation[1]
            assert isinstance(hours, int)
            self.now += timedelta(hours=hours)
            self.book.sweep_expired(self.now)
        elif self.lots:
            pick, more_reversed, more_restored = operation[1:]
            assert isinstance(pick, int)
            assert isinstance(more_reversed, int) and isinstance(more_restored, int)
            lot_id = self.lots[pick % len(self.lots)]
            reversed_cents, restored = self.cents[lot_id]
            reversed_cents = min(_TOPUP_SETTLED, reversed_cents + more_reversed)
            restored = min(reversed_cents, restored + more_restored)
            self.cents[lot_id] = [reversed_cents, restored]
            self.book.sweep_expired(self.now)
            self.targets(lot_id)

    def check(self) -> None:
        book = self.book
        fold = fold_ledger(
            LedgerMovement(e["entry_type"], e["amount_mc"], e["lot_id"])
            for e in book.writes.entries
        )
        assert fold.debt_mc == book.open_debt_mc
        for lot in book.lots:
            assert lot.balance_mc >= 0
            if lot.expired(self.now):
                assert lot.balance_mc == 0, "an expired lot holds capacity"
        if book.open_debt_mc:
            assert book.available_mc(self.now) == 0
        assert book.open_debt_mc <= self.consumed
        for lot in book.lots:  # the release cascade keeps the per-line rules
            assert book.reversal_debt_net_mc(lot.id) <= lot.consumed_mc
            if lot.available_mc(self.now) > 0:
                assert book.reversal_debt_net_mc(lot.id) == 0

    def position(self) -> tuple[int, int, int]:
        book = self.book
        return (
            sum(lot.balance_mc for lot in book.lots),
            book.available_mc(self.now),
            book.open_debt_mc,
        )


@settings(max_examples=EXAMPLES, deadline=None)
@given(
    _OPERATIONS,
    st.integers(1, 20),
    st.integers(1, _TOPUP_SETTLED),
    _OPERATIONS,
)
def test_a_chargeback_won_at_once_changes_nothing(
    prefix: list[tuple[object, ...]],
    credits: int,
    cents: int,
    suffix: list[tuple[object, ...]],
) -> None:
    with_chargeback, without = _Run(), _Run()
    for run in (with_chargeback, without):
        for operation in prefix:
            run.step(operation)
            run.check()
        run.grant(credits, -10)
        run.won = run.lots[-1]
        run.step(("debit", 3_000))
    line = with_chargeback.lots[-1]
    with_chargeback.cents[line] = [cents, 0]
    with_chargeback.targets(line)
    with_chargeback.check()
    with_chargeback.cents[line] = [cents, cents]
    with_chargeback.targets(line)
    with_chargeback.check()
    assert with_chargeback.position() == without.position()
    for run in (with_chargeback, without):
        for operation in suffix:
            if (
                operation[0] == "rev"
                and run.lots
                and run.lots[int(str(operation[1])) % len(run.lots)] == run.won
            ):
                continue  # the won line stays untouched afterwards
            run.step(operation)
            run.check()
    assert with_chargeback.position() == without.position()
