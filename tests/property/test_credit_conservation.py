"""Millicredit conservation, monthly-then-FEFO order and no expired use.

Random sequences of grants (monthly and top-ups), debits, time advances and
cumulative reversal/restoration targets drive one pure
:class:`~src.services.credit_ledger.LedgerBook`.  After every step:

* the rows the book recorded fold (``fold_ledger``) to exactly the balances
  and debt it tracks; no lot is negative, no debt is negative or over-paid;
* open debt and usable capacity never coexist (debt is settled first);
* while nothing has expired, ``usable - open debt = granted - consumed -
  net reversed`` exactly; with expiry the difference only loses what
  expired: ``granted - consumed - net reversed - expired <= usable - debt <=
  granted - consumed - net reversed``;
* a debit consumes the monthly allowance first, then top-ups by expiry,
  ``paid_at`` and id, greedily, and never a lot expired at that instant;
* a lot past its deadline holds nothing; open debt never exceeds what was
  consumed, and -- with the release cascade -- no line owes more reversal
  debt than it consumed; every cascaded release withdrew what it released;
* no line holds usable capacity while its own reversal debt is in force, and
  per line (nothing expired) holding + consumed - reversal debt in force =
  granted - net reversed;
* identical histories give identical ledgers, whatever random ids the lots
  got (every tie is broken by a stable value, release ids digest their
  trigger), also with writers whose clock lags (they act at the ledger's
  clock); a book reloaded from its rows at every step (one transaction per
  fact) gives the same ledger as one kept in memory.

``CREDIT_PROPERTY_EXAMPLES`` raises the example count of the heavy
properties (default 300; the review runs 3000).

Chargebacks (full reversal) and wins (full restoration) are generated next
to partial steps so debt release and settlement release are frequent.  The
exact effect of time is pinned by the differential properties of
``test_credit_reversal_restoration.py``.
"""

from __future__ import annotations

import hashlib
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from hypothesis import event, given, settings
from hypothesis import strategies as st

from src.repositories.credit import LedgerMovement, fold_ledger
from src.services.credit_ledger import (
    LEDGER_EFFECT_KINDS,
    GrantSpec,
    LedgerBook,
    LedgerWrites,
    add_calendar_months,
    assemble_book,
    cumulative_target_mc,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    topup_grant_effect_id,
    topup_lot_expires_at,
)

EXAMPLES = int(os.environ.get("CREDIT_PROPERTY_EXAMPLES", "300"))
START = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
BASE_DAY = date(2026, 10, 1)
SUBSCRIPTION = UUID("5a000000-0000-4000-8000-00000000000b")
SETTLED_CENTS = 10_000

OPERATIONS = st.lists(
    st.one_of(
        st.tuples(st.just("monthly"), st.integers(1, 40), st.integers(0, 3)),
        st.tuples(st.just("topup"), st.integers(1, 40), st.integers(-200, 0)),
        st.tuples(st.just("debit"), st.integers(1, 60_000)),
        st.tuples(st.just("advance"), st.integers(1, 24 * 40)),
        st.tuples(st.just("reverse"), st.integers(0, 50), st.integers(0, 4_000)),
        st.tuples(st.just("restore"), st.integers(0, 50), st.integers(0, 4_000)),
        st.tuples(st.just("replay"), st.integers(0, 50)),
        st.tuples(st.just("chargeback"), st.integers(0, 50)),
        st.tuples(st.just("win"), st.integers(0, 50)),
    ),
    max_size=40,
)


@dataclass
class _Run:
    book: LedgerBook = field(
        default_factory=lambda: LedgerBook(builder_id=uuid4(), account_id=uuid4())
    )
    now: datetime = START
    granted: int = 0
    consumed: int = 0
    lots: list[UUID] = field(default_factory=list)
    cents: dict[UUID, list[int]] = field(default_factory=dict)  # [reversed, restored]
    cycles: set[int] = field(default_factory=set)
    serial: int = 0
    # ``reload``: every step is its own transaction -- the book is rebuilt
    # from the rows of the previous ones with the adapter's own loader.
    reload: bool = False
    stored: LedgerWrites = field(default_factory=LedgerWrites)

    @property
    def entries(self) -> list[dict[str, Any]]:
        return self.stored.entries + self.book.writes.entries

    @property
    def effects(self) -> list[dict[str, Any]]:
        return self.stored.effects + self.book.writes.effects

    def begin(self) -> None:
        if not self.reload:
            return
        writes, stored = self.book.writes, self.stored
        for name in ("effects", "lots", "entries", "debts", "settlements"):
            getattr(stored, name).extend(getattr(writes, name))
        sums: dict[UUID, Counter[str]] = {}
        for entry in stored.entries:
            if entry["lot_id"] is not None:
                sums.setdefault(entry["lot_id"], Counter())[
                    entry["entry_type"]
                ] += entry["amount_mc"]
        settled: dict[UUID, Counter[UUID]] = {}
        for row in stored.settlements:
            settled.setdefault(row["debt_id"], Counter())[
                row["settling_grant_lot_id"]
            ] += row["amount_mc"]
        self.book = assemble_book(
            builder_id=self.book.builder_id,
            account_id=self.book.account_id,
            lots=stored.lots,
            lot_sums=sums,
            debts=stored.debts,
            settled=settled,
            known_effects=[
                row["effect_id"]
                for row in stored.effects
                if row["effect_kind"] in LEDGER_EFFECT_KINDS
            ],
            release_rows=[
                (e["effect_id"], e["entry_type"], e["lot_id"], e["amount_mc"])
                for e in stored.entries
                if e["entry_type"] in ("debt_release", "settlement_release")
            ],
            ledger_debt_mc=fold_ledger(
                LedgerMovement(e["entry_type"], e["amount_mc"], e["lot_id"])
                for e in stored.entries
            ).debt_mc,
            clock_floor=max((e["occurred_at"] for e in stored.entries), default=None),
        )
        self.book.sweep_expired(self.now)

    def net_of(self, lot_id: UUID) -> int:
        reversed_cents, restored = self.cents[lot_id]
        grant = self.book.lot(lot_id).granted_mc
        return cumulative_target_mc(
            grant, reversed_cents, SETTLED_CENTS
        ) - cumulative_target_mc(grant, restored, SETTLED_CENTS)

    def net_reversed(self) -> int:
        total = 0
        for lot_id, (reversed_cents, restored) in self.cents.items():
            grant = self.book.lot(lot_id).granted_mc
            total += cumulative_target_mc(grant, reversed_cents, SETTLED_CENTS)
            total -= cumulative_target_mc(grant, restored, SETTLED_CENTS)
        return total

    def apply_targets(self, lot_id: UUID) -> None:
        reversed_cents, restored = self.cents[lot_id]
        grant = self.book.lot(lot_id).granted_mc
        self.book.apply_line_targets(
            lot_id,
            line_id="line",
            payment_id=f"pay-{lot_id}",
            financial_revision=self.serial,
            reversal_target_mc=cumulative_target_mc(
                grant, reversed_cents, SETTLED_CENTS
            ),
            restoration_target_mc=cumulative_target_mc(grant, restored, SETTLED_CENTS),
            at=self.now,
        )

    def step(self, operation: tuple[object, ...]) -> None:
        self.serial += 1
        self.begin()
        kind = operation[0]
        if kind == "monthly":
            _, credits, cycle = operation
            assert isinstance(credits, int) and isinstance(cycle, int)
            if cycle in self.cycles:
                return
            self.cycles.add(cycle)
            start = add_calendar_months(BASE_DAY, cycle)
            end = add_calendar_months(start, 1)
            lot = self.book.grant(
                GrantSpec(
                    lot_kind="monthly",
                    effect_id=monthly_grant_effect_id(SUBSCRIPTION, start, end),
                    payment_id=f"pay-cycle-{cycle}",
                    paid_at=start,
                    granted_mc=credits * 1_000,
                    expires_at=monthly_lot_expires_at(end),
                    subscription_id=SUBSCRIPTION,
                    cycle_start=start,
                    cycle_end=end,
                ),
                at=self.now,
            )
            self._granted(lot.id, credits * 1_000)
        elif kind == "topup":
            _, credits, offset = operation
            assert isinstance(credits, int) and isinstance(offset, int)
            order_id = UUID(int=0xA0000 + self.serial)  # a stable input
            paid_at = BASE_DAY + timedelta(days=offset)
            lot = self.book.grant(
                GrantSpec(
                    lot_kind="topup",
                    effect_id=topup_grant_effect_id(order_id),
                    payment_id=f"pay-{order_id}",
                    paid_at=paid_at,
                    granted_mc=credits * 1_000,
                    expires_at=topup_lot_expires_at(paid_at),
                    order_id=order_id,
                ),
                at=self.now,
            )
            self._granted(lot.id, credits * 1_000)
        elif kind == "debit":
            amount = operation[1]
            assert isinstance(amount, int)
            self._debit(amount)
        elif kind == "advance":
            hours = operation[1]
            assert isinstance(hours, int)
            self.now += timedelta(hours=hours)
            self.book.sweep_expired(self.now)
        elif self.lots:
            pick = operation[1]
            assert isinstance(pick, int)
            lot_id = self.lots[pick % len(self.lots)]
            reversed_cents, restored = self.cents[lot_id]
            if kind == "reverse":
                increment = operation[2]
                assert isinstance(increment, int)
                reversed_cents = min(SETTLED_CENTS, reversed_cents + increment)
            elif kind == "restore":
                increment = operation[2]
                assert isinstance(increment, int)
                restored = min(reversed_cents, restored + increment)
            elif kind == "chargeback":
                reversed_cents = SETTLED_CENTS
            elif kind == "win":
                restored = reversed_cents
            self.cents[lot_id] = [reversed_cents, restored]
            self.book.sweep_expired(self.now)
            self.apply_targets(lot_id)

    def _granted(self, lot_id: UUID, amount: int) -> None:
        self.granted += amount
        self.lots.append(lot_id)
        self.cents[lot_id] = [0, 0]

    def _debit(self, amount: int) -> None:
        book = self.book
        before = len(book.writes.entries)
        usable = [
            (lot.id, lot.available_mc(self.now))
            for lot in book.lots
            if lot.available_mc(self.now) > 0
        ]
        kinds = [book.lot(lot_id).lot_kind for lot_id, _ in usable]
        assert kinds == sorted(kinds, key=lambda k: k != "monthly")
        effect_id = f"trace:{self.serial}"
        result = book.debit(effect_id, amount, at=self.now, effect_kind="trace")
        assert result.status == "applied"
        self.consumed += amount
        expected: list[tuple[UUID, int]] = []
        remaining = amount
        for lot_id, available in usable:
            if remaining == 0:
                break
            take = min(remaining, available)
            expected.append((lot_id, take))
            remaining -= take
        debits = [
            (entry["lot_id"], entry["amount_mc"])
            for entry in book.writes.entries[before:]
            if entry["entry_type"] == "debit"
        ]
        assert debits == expected
        assert result.debt_mc == remaining


def _check(run: _Run, *, expired_anything: bool) -> None:
    book = run.book
    fold = fold_ledger(
        LedgerMovement(e["entry_type"], e["amount_mc"], e["lot_id"])
        for e in run.entries
    )
    tracked = {lot.id: lot.balance_mc for lot in book.lots}
    assert {k: v for k, v in fold.lots.items()} == {
        k: v for k, v in tracked.items() if k in fold.lots
    }
    assert fold.debt_mc == book.open_debt_mc
    assert all(balance >= 0 for balance in tracked.values())
    open_amounts = book.open_debt_by_id()
    for debt in book.debts:
        assert 0 <= open_amounts[debt.id] <= debt.amount_mc - debt.settled_mc
    usable = book.available_mc(run.now)
    if book.open_debt_mc:
        assert usable == 0
    expected = run.granted - run.consumed - run.net_reversed()
    expired = sum(e["amount_mc"] for e in run.entries if e["entry_type"] == "expiry")
    position = usable - book.open_debt_mc
    if expired_anything:
        assert expected - expired <= position <= expected
    else:
        assert position == expected
    lots = {lot.id: lot for lot in book.lots}
    for entry in run.entries:
        if entry["entry_type"] in ("debit", "debt_settlement"):
            assert lots[entry["lot_id"]].expires_at > entry["occurred_at"]
        assert entry["amount_mc"] > 0
    for lot in book.lots:
        if lot.expired(run.now):
            assert lot.balance_mc == 0, "an expired lot holds capacity"
    # Debt only ever stands for consumption: open debt overall, and the
    # reversal debt in force of each line (the release cascade returns a
    # payment to the lot that really paid, never to a charged-back payer).
    assert book.open_debt_mc <= run.consumed
    for lot in book.lots:
        assert book.reversal_debt_net_mc(lot.id) <= lot.consumed_mc
    # Every cascaded release withdrew exactly what its debts' returns
    # released; every release names a debt (``return:`` effects only).
    trigger = _triggers(run)
    released: Counter[str] = Counter()
    withdrawn: Counter[str] = Counter()
    for entry in run.entries:
        effect = str(entry["effect_id"])
        if effect.startswith("release:") and entry["entry_type"] == "reversal":
            withdrawn[effect] += int(entry["amount_mc"])
        elif entry["entry_type"] in ("debt_release", "settlement_release"):
            assert effect.startswith("return:"), "a release without its debt"
            if trigger[effect].startswith("release:"):
                released[trigger[effect]] += int(entry["amount_mc"])
    assert released == withdrawn
    # No line holds usable capacity while its own reversal debt is in force
    # (owner decision E); per line, while nothing expired, what it holds,
    # consumed and owes closes on its funding in force.
    for lot in book.lots:
        in_force = book.reversal_debt_net_mc(lot.id)
        if lot.available_mc(run.now) > 0:
            assert in_force == 0, "a line holds capacity while it owes reversal"
        if not expired_anything:
            assert lot.balance_mc + lot.consumed_mc - in_force == (
                lot.granted_mc - run.net_of(lot.id)
            )


@settings(max_examples=EXAMPLES, deadline=None)
@given(OPERATIONS)
def test_conservation_and_order_hold_for_any_sequence(
    operations: list[tuple[object, ...]],
) -> None:
    run = _Run()
    for operation in operations:
        run.step(operation)
        expired_anything = any(lot.expired(run.now) for lot in run.book.lots) or any(
            e["entry_type"] == "expiry" for e in run.entries
        )
        _check(run, expired_anything=expired_anything)
    for entry_type in sorted({e["entry_type"] for e in run.entries}):
        event(f"wrote {entry_type}")


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.one_of(
            st.tuples(st.just("monthly"), st.integers(1, 40), st.just(0)),
            st.tuples(st.just("topup"), st.integers(1, 40), st.just(0)),
            st.tuples(st.just("debit"), st.integers(1, 60_000)),
            st.tuples(st.just("advance"), st.integers(1, 48)),
            st.tuples(st.just("reverse"), st.integers(0, 50), st.integers(0, 4_000)),
            st.tuples(st.just("restore"), st.integers(0, 50), st.integers(0, 4_000)),
            st.tuples(st.just("replay"), st.integers(0, 50)),
            st.tuples(st.just("chargeback"), st.integers(0, 50)),
            st.tuples(st.just("win"), st.integers(0, 50)),
        ),
        max_size=40,
    )
)
def test_without_expiry_the_position_is_exactly_conserved(
    operations: list[tuple[object, ...]],
) -> None:
    run = _Run()
    for operation in operations:
        run.step(operation)
        assert not any(lot.expired(run.now) for lot in run.book.lots)
        _check(run, expired_anything=False)


@st.composite
def _chargeback_scenarios(draw: st.DrawFn) -> list[tuple[object, ...]]:
    """Grants, heavy consumption, a chargeback, maybe time, its win, then
    anything: the shape that makes debt and settlement releases common."""

    grants: list[tuple[object, ...]] = [
        draw(
            st.one_of(
                st.tuples(st.just("monthly"), st.integers(1, 40), st.integers(0, 1)),
                st.tuples(st.just("topup"), st.integers(1, 40), st.integers(-200, 0)),
            )
        )
        for _ in range(draw(st.integers(1, 4)))
    ]
    pick = draw(st.integers(0, 50))
    middle: list[tuple[object, ...]] = [
        ("debit", draw(st.integers(1, 120_000))),
        ("chargeback", pick),
    ]
    if draw(st.booleans()):
        middle.append(("advance", draw(st.integers(1, 24 * 200))))
    if draw(st.booleans()):
        middle.append(("topup", draw(st.integers(1, 40)), 0))
    middle.append(("win", pick))
    return grants + middle + draw(OPERATIONS.map(lambda ops: ops[:10]))


@st.composite
def _cascade_scenarios(draw: st.DrawFn) -> list[tuple[object, ...]]:
    """A line charged back, a payer settling its debt and then charged back
    itself, maybe a third lot paying that, then the line's win: the shape
    that makes the release cascade (and its ``release:`` ids) common."""

    grants: list[tuple[object, ...]] = [
        ("topup", draw(st.integers(1, 40)), draw(st.integers(-200, 0)))
        for _ in range(draw(st.integers(1, 3)))
    ]
    line = draw(st.integers(0, len(grants) - 1))  # stable as lots are added
    middle: list[tuple[object, ...]] = [
        ("debit", draw(st.integers(1, 120_000))),
        ("chargeback", line),
        ("topup", draw(st.integers(1, 40)), 0),  # the payer (the last lot)
    ]
    if draw(st.booleans()):
        middle.append(("debit", draw(st.integers(1, 40_000))))
    if draw(st.booleans()):
        middle.append(("chargeback", -1))
    else:
        middle.append(("reverse", -1, draw(st.integers(1, SETTLED_CENTS))))
    if draw(st.booleans()):
        middle.append(("topup", draw(st.integers(1, 40)), 0))
    if draw(st.booleans()):
        middle.append(("advance", draw(st.integers(1, 24 * 200))))
    middle.append(("win", line))
    return grants + middle + draw(OPERATIONS.map(lambda ops: ops[:6]))


SCENARIOS = st.one_of(OPERATIONS, _chargeback_scenarios(), _cascade_scenarios())


@settings(max_examples=EXAMPLES, deadline=None)
@given(st.one_of(_chargeback_scenarios(), _cascade_scenarios()))
def test_chargeback_scenarios_keep_every_invariant(
    operations: list[tuple[object, ...]],
) -> None:
    run = _Run()
    for operation in operations:
        run.step(operation)
        expired_anything = any(lot.expired(run.now) for lot in run.book.lots) or any(
            e["entry_type"] == "expiry" for e in run.entries
        )
        _check(run, expired_anything=expired_anything)
    for entry_type in sorted({e["entry_type"] for e in run.entries}):
        event(f"wrote {entry_type}")


def _triggers(run: _Run) -> dict[str, str]:
    """``return:``/``release:`` effect -> the effect that set it off."""

    by_digest = {
        hashlib.sha256(str(row["effect_id"]).encode("utf-8")).hexdigest(): str(
            row["effect_id"]
        )
        for row in run.effects
    }
    return {
        str(row["effect_id"]): by_digest[str(row["effect_id"]).rpartition(":")[2]]
        for row in run.effects
        if str(row["effect_id"]).startswith(("return:", "release:"))
    }


def _signature(run: _Run) -> list[tuple[object, ...]]:
    """The ledger with every random lot id replaced by its grant effect id,
    every debt id by the effect that created it and every return/release
    digest by the effect that triggered it."""

    names = {lot.id: lot.grant_effect_id for lot in run.book.lots}
    debts = {debt.id: debt.source_effect_id for debt in run.book.debts}
    trigger = _triggers(run)

    def stable(effect_id: str) -> str:
        kind, _, rest = effect_id.partition(":")
        if kind == "release":
            lot_text = rest.partition(":")[0]
            parent = stable(trigger[effect_id])
            return f"release:{names[UUID(lot_text)]}:<{parent}>"
        if kind == "return":
            debt_text = rest.partition(":")[0]
            parent = stable(trigger[effect_id])
            return f"return:<{stable(debts[UUID(debt_text)])}>:<{parent}>"
        for lot_id, name in names.items():
            effect_id = effect_id.replace(str(lot_id), name)
        return effect_id

    rows: list[tuple[object, ...]] = [
        (
            entry["entry_type"],
            entry["amount_mc"],
            names.get(entry["lot_id"]),
            stable(str(entry["effect_id"])),
            entry["occurred_at"],
        )
        for entry in run.entries
    ]
    rows.append(("open-debt", run.book.open_debt_mc))
    rows.append(("usable", run.book.available_mc(run.now)))
    return rows


@settings(max_examples=EXAMPLES, deadline=None)
@given(SCENARIOS)
def test_identical_histories_give_identical_ledgers(
    operations: list[tuple[object, ...]],
) -> None:
    first, second = _Run(), _Run()  # the lots get different random ids
    for operation in operations:
        first.step(operation)
        second.step(operation)
    assert _signature(first) == _signature(second)


@settings(max_examples=EXAMPLES, deadline=None)
@given(SCENARIOS)
def test_a_reloaded_book_behaves_like_the_continuous_one(
    operations: list[tuple[object, ...]],
) -> None:
    """One transaction per step (the adapter's loader rebuilds the book from
    the rows each time) gives the same ledger as one book kept in memory."""

    continuous, reloaded = _Run(), _Run(reload=True)
    for operation in operations:
        continuous.step(operation)
        reloaded.step(operation)
        expired_anything = any(
            lot.expired(reloaded.now) for lot in reloaded.book.lots
        ) or any(e["entry_type"] == "expiry" for e in reloaded.entries)
        _check(reloaded, expired_anything=expired_anything)
    assert _signature(reloaded) == _signature(continuous)


@st.composite
def _owing_line(draw: st.DrawFn) -> list[tuple[object, ...]]:
    """Line 0 consumed, then reversed in steps whose debt other lots settle
    (grants and time between, sometimes a partial win and a further
    reversal): a line that already carries debt settled by other payers."""

    ops: list[tuple[object, ...]] = [
        ("topup", draw(st.integers(1, 40)), draw(st.integers(-200, 0))),
        ("debit", draw(st.integers(1, 60_000))),
    ]
    for _ in range(draw(st.integers(1, 3))):
        ops.append(
            draw(
                st.one_of(
                    st.tuples(
                        st.just("monthly"), st.integers(1, 40), st.integers(0, 3)
                    ),
                    st.tuples(
                        st.just("topup"), st.integers(1, 40), st.integers(-200, 0)
                    ),
                )
            )
        )
        if draw(st.booleans()):
            ops.append(("advance", draw(st.integers(1, 24 * 60))))
        ops.append(("reverse", 0, draw(st.integers(1, 4_000))))
        if draw(st.booleans()):
            ops.append(("debit", draw(st.integers(1, 20_000))))
        if draw(st.integers(0, 2)) == 0:
            ops.append(("restore", 0, draw(st.integers(1, 2_000))))
            ops.append(("reverse", 0, draw(st.integers(1, 2_000))))
    return ops


def _by_grant(run: _Run) -> dict[str, int]:
    return {lot.grant_effect_id: lot.balance_mc for lot in run.book.lots}


@settings(max_examples=EXAMPLES, deadline=None)
@given(
    st.one_of(
        st.tuples(_owing_line(), st.just(0)),
        st.tuples(SCENARIOS, st.integers(0, 50)),
    ),
    st.integers(1, SETTLED_CENTS),
    st.booleans(),
)
def test_a_reversal_won_at_once_changes_no_lot(
    prefix_and_pick: tuple[list[tuple[object, ...]], int], cents: int, reload: bool
) -> None:
    """Any existing line -- typically one already owing debt that other lots
    settled -- reversed by d cents and won back by d cents at once leaves
    every lot balance and the open debt as they were (the reviewer's R4-1:
    the restoration returns the newest debt first, to the lots that paid
    THAT debt)."""

    prefix, pick = prefix_and_pick
    runs = []
    for with_pair in (True, False):
        run = _Run(reload=reload)
        for operation in prefix:
            run.step(operation)
        if not run.lots:
            return
        index = pick % len(run.lots)
        lot_id = run.lots[index]
        reversed_cents, restored = run.cents[lot_id]
        delta = min(cents, SETTLED_CENTS - reversed_cents)
        grant = run.book.lot(lot_id).granted_mc
        if delta <= 0 or cumulative_target_mc(
            grant, reversed_cents + delta, SETTLED_CENTS
        ) - cumulative_target_mc(grant, reversed_cents, SETTLED_CENTS) != (
            cumulative_target_mc(grant, restored + delta, SETTLED_CENTS)
            - cumulative_target_mc(grant, restored, SETTLED_CENTS)
        ):
            return  # floor rounding: not the same millicredit delta
        if with_pair:
            run.step(("reverse", index, delta))
            run.step(("restore", index, delta))
            event("won at once")
            if any(e["entry_type"] == "settlement_release" for e in run.entries):
                event("won at once returned settled debt")
        else:
            run.step(("advance", 0))
            run.step(("advance", 0))
        runs.append(run)
    with_pair_run, without = runs
    assert _by_grant(with_pair_run) == _by_grant(without)
    assert with_pair_run.book.open_debt_mc == without.book.open_debt_mc


LAGS = st.integers(0, 6 * 3600)


@settings(max_examples=EXAMPLES, deadline=None)
@given(
    st.lists(
        st.tuples(st.one_of(OPERATIONS, _chargeback_scenarios()), LAGS), max_size=6
    )
)
def test_writers_whose_clock_lags_keep_every_invariant(
    batches: list[tuple[list[tuple[object, ...]], int]],
) -> None:
    """Each batch runs as a writer ``lag`` seconds behind (adopted from the
    2026-10-26 review): it acts at the ledger's clock instead."""

    run = _Run()
    for operations, lag in batches:
        for operation in operations:
            true_now = run.now
            if operation[0] != "advance":
                run.now = true_now - timedelta(seconds=lag)
            run.step(operation)
            run.now = max(true_now, run.book.clock or true_now)
            expired_anything = any(
                lot.expired(run.now) for lot in run.book.lots
            ) or any(e["entry_type"] == "expiry" for e in run.entries)
            _check(run, expired_anything=expired_anything)


def test_the_two_builders_never_share_a_row() -> None:
    first, second = _Run(), _Run()
    for run in (first, second):
        run.step(("topup", 5, 0))
        run.step(("debit", 7_000))
    for run in (first, second):
        builder = run.book.builder_id
        rows = (
            run.book.writes.effects
            + run.book.writes.lots
            + run.book.writes.entries
            + run.book.writes.debts
        )
        assert {row["builder_id"] for row in rows} == {builder}
    assert not set(first.lots) & set(second.lots)
    assert Counter(e["entry_type"] for e in first.book.writes.entries) == Counter(
        {"grant": 1, "debit": 1, "debt": 1}
    )
