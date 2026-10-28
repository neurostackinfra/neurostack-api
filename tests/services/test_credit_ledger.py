"""Credit ledger rules (neurostack-credit-system Group 2).

The pure :class:`LedgerBook` (no I/O) and its SQLite round trip through
:class:`CreditLedger`; PostgreSQL proofs (locks, triggers, concurrency) live
in ``tests/integration/test_credit_ledger_postgres.py``.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.models.base import Base
from src.models.billing import BillingTopupOrder
from src.models.credit import (
    CreditDebt,
    CreditDebtOrigin,
    CreditDebtSettlement,
    CreditEffect,
    CreditEnforcementState,
    CreditLedgerEntry,
    CreditLot,
)
from src.repositories.credit import CreditRepository, LedgerMovement, fold_ledger
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import credit_ledger as ledger_module
from src.services.credit_ledger import (
    CREDIT_TIMEZONE,
    CreditLedger,
    DebitResult,
    DebtState,
    GrantSpec,
    LedgerBook,
    LedgerClockAhead,
    LedgerInvariantError,
    LotState,
    SpendHold,
    add_calendar_months,
    civil_date,
    cumulative_target_mc,
    cycle_start_settlement_effect_id,
    expiry_effect_id,
    monthly_grant_effect_id,
    monthly_lot_expires_at,
    parse_line_effect_id,
    restoration_effect_id,
    reversal_effect_id,
    start_of_civil_day,
    topup_grant_effect_id,
    topup_lot_expires_at,
    topup_lot_expires_on,
)

T0 = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)
# Not all digits: SQLite gives a ``UUID`` column NUMERIC affinity and would
# store an all-digit hex string as a number (PostgreSQL has a real uuid type).
SUB = UUID("5a000000-0000-4000-8000-00000000000b")


# ---------------------------------------------------------------------------
# Calendar and money rules.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paid_at", "months", "expected"),
    [
        (date(2026, 10, 12), 6, date(2027, 4, 12)),
        (date(2026, 8, 31), 6, date(2027, 2, 28)),
        (date(2027, 8, 31), 6, date(2028, 2, 29)),  # leap year
        (date(2026, 3, 31), 6, date(2026, 9, 30)),
        (date(2026, 12, 31), 2, date(2027, 2, 28)),
        (date(2026, 1, 31), 0, date(2026, 1, 31)),
    ],
)
def test_calendar_months_clamp_to_the_last_day(
    paid_at: date, months: int, expected: date
) -> None:
    assert add_calendar_months(paid_at, months) == expected


def test_calendar_months_refuse_negative() -> None:
    with pytest.raises(ValueError):
        add_calendar_months(date(2026, 1, 1), -1)


@pytest.mark.parametrize(
    ("day", "utc"),
    [
        (date(2026, 11, 12), datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)),
        # DST start at 00:00: midnight did not exist, the day began at 01:00 -02.
        (date(2018, 11, 4), datetime(2018, 11, 4, 3, 0, tzinfo=timezone.utc)),
        # Inside DST (-02).
        (date(2019, 1, 15), datetime(2019, 1, 15, 2, 0, tzinfo=timezone.utc)),
        # DST end: 23:00-23:59 of the previous day happened twice.
        (date(2019, 2, 17), datetime(2019, 2, 17, 3, 0, tzinfo=timezone.utc)),
    ],
)
def test_start_of_civil_day_in_sao_paulo(day: date, utc: datetime) -> None:
    instant = start_of_civil_day(day)
    assert instant == utc and instant.utcoffset() == timedelta(0)
    assert civil_date(instant) == day
    assert civil_date(instant - timedelta(microseconds=1)) == day - timedelta(days=1)


def test_lot_expiry_instants() -> None:
    assert topup_lot_expires_on(date(2026, 10, 12)) == date(2027, 4, 12)
    assert topup_lot_expires_at(date(2026, 10, 12)) == datetime(
        2027, 4, 12, 3, 0, tzinfo=timezone.utc
    )
    assert monthly_lot_expires_at(date(2026, 11, 12)) == datetime(
        2026, 11, 12, 3, 0, tzinfo=timezone.utc
    )
    with pytest.raises(ValueError):
        civil_date(datetime(2026, 1, 1))
    assert str(CREDIT_TIMEZONE) == "America/Sao_Paulo"


@pytest.mark.parametrize(
    ("grant", "cents", "settled", "expected"),
    [
        (5_000_000, 99_900, 99_900, 5_000_000),
        (500_000, 5_000, 10_000, 250_000),
        (500_000, 2_500, 10_000, 125_000),
        (5_000_000, 1, 99_900, 50),  # floor(5_000_000 / 99_900)
        (5_000_000, 0, 99_900, 0),
    ],
)
def test_cumulative_targets_floor_in_integers(
    grant: int, cents: int, settled: int, expected: int
) -> None:
    assert cumulative_target_mc(grant, cents, settled) == expected


@pytest.mark.parametrize(
    ("grant", "cents", "settled"),
    [(0, 1, 1), (10, 2, 1), (10, -1, 1), (10, 1, 0), (10.0, 1, 1)],
)
def test_cumulative_targets_refuse_invalid_inputs(
    grant: object, cents: int, settled: int
) -> None:
    with pytest.raises(ValueError):
        cumulative_target_mc(grant, cents, settled)  # type: ignore[arg-type]


def test_line_effect_ids_carry_the_cumulative_target() -> None:
    lot_id = uuid4()
    reversal = reversal_effect_id(
        lot_id,
        line_id="plan",
        payment_id="pay:with:colons",
        financial_revision=2,
        target_mc=250_000,
    )
    restoration = restoration_effect_id(
        lot_id, line_id="plan", payment_id="pay_x", financial_revision=3, target_mc=7
    )
    assert parse_line_effect_id(reversal) == ("reversal", lot_id, 250_000)
    assert parse_line_effect_id(restoration) == ("restoration", lot_id, 7)
    assert parse_line_effect_id(expiry_effect_id(lot_id)) is None
    assert parse_line_effect_id(f"reversal:{lot_id}:plan:pay:1:x") is None
    assert parse_line_effect_id("reversal:not-a-uuid:plan:pay:1:5") is None
    assert len(reversal) <= 512
    assert monthly_grant_effect_id(SUB, date(2026, 10, 12), date(2026, 11, 12)) == (
        f"cycle:{SUB}:2026-10-12:2026-11-12:grant"
    )
    assert topup_grant_effect_id(SUB) == f"topup:{SUB}:grant"


# ---------------------------------------------------------------------------
# The pure book.
# ---------------------------------------------------------------------------


def _book(**options: object) -> LedgerBook:
    return LedgerBook(builder_id=uuid4(), account_id=uuid4(), **options)  # type: ignore[arg-type]


def _monthly(
    book: LedgerBook,
    *,
    grant: int = 5_000_000,
    start: date = date(2026, 10, 12),
    end: date = date(2026, 11, 12),
    at: datetime = T0,
) -> UUID:
    lot = book.grant(
        GrantSpec(
            lot_kind="monthly",
            effect_id=monthly_grant_effect_id(SUB, start, end),
            payment_id=f"pay-{start}",
            paid_at=start,
            granted_mc=grant,
            expires_at=monthly_lot_expires_at(end),
            subscription_id=SUB,
            cycle_start=start,
            cycle_end=end,
        ),
        at=at,
    )
    return lot.id


def _topup(
    book: LedgerBook,
    *,
    grant: int = 500_000,
    paid_at: date = date(2026, 10, 12),
    at: datetime = T0,
    order_id: UUID | None = None,
) -> UUID:
    order_id = order_id or uuid4()
    lot = book.grant(
        GrantSpec(
            lot_kind="topup",
            effect_id=topup_grant_effect_id(order_id),
            payment_id=f"pay-{order_id}",
            paid_at=paid_at,
            granted_mc=grant,
            expires_at=topup_lot_expires_at(paid_at),
            order_id=order_id,
        ),
        at=at,
    )
    return lot.id


def _targets(
    book: LedgerBook, lot_id: UUID, reversal: int, restoration: int = 0, rev: int = 2
) -> object:
    return book.apply_line_targets(
        lot_id,
        line_id="plan",
        payment_id="pay",
        financial_revision=rev,
        reversal_target_mc=reversal,
        restoration_target_mc=restoration,
        at=T0 + timedelta(hours=rev),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _of_effects(book: LedgerBook, effect_prefix: str) -> list[dict[str, Any]]:
    """Entries of the effects with this prefix and of the ``return:``
    effects (one released debt each) they set off directly."""

    roots = {e for e in book.writes.effect_ids if e.startswith(effect_prefix)}
    digests = {_sha(e) for e in roots}
    return [
        entry
        for entry in book.writes.entries
        if str(entry["effect_id"]) in roots
        or (
            str(entry["effect_id"]).startswith("return:")
            and str(entry["effect_id"]).rpartition(":")[2] in digests
        )
    ]


def _by_effect(book: LedgerBook, effect_prefix: str) -> Counter[str]:
    totals: Counter[str] = Counter()
    for entry in _of_effects(book, effect_prefix):
        totals[str(entry["entry_type"])] += int(entry["amount_mc"])
    return totals


def test_grant_creates_one_lot_with_its_entry() -> None:
    book = _book()
    lot_id = _monthly(book)
    assert book.lot(lot_id).balance_mc == 5_000_000
    assert [e["entry_type"] for e in book.writes.entries] == ["grant"]
    assert book.writes.effect_ids == (
        monthly_grant_effect_id(SUB, date(2026, 10, 12), date(2026, 11, 12)),
    )
    with pytest.raises(LedgerInvariantError):
        _monthly(book)


def test_debit_consumes_monthly_first_then_fefo_and_skips_expired() -> None:
    book = _book()
    late = _topup(book, paid_at=date(2026, 10, 1), order_id=UUID(int=2))
    early = _topup(book, paid_at=date(2026, 9, 1), order_id=UUID(int=3))
    same_day_a = _topup(book, paid_at=date(2026, 9, 1), order_id=UUID(int=4))
    expired = _topup(book, paid_at=date(2025, 1, 1), order_id=UUID(int=5))
    monthly = _monthly(book)
    assert book.lot(expired).balance_mc == 0  # granted, then expired at once
    order = [lot.id for lot in book.lots if lot.available_mc(T0) > 0]
    assert order[0] == monthly
    assert order[-1] == late
    assert order[1:3] == [early, same_day_a]  # paid_at tie -> grant effect id

    result = book.debit("trace:1", 5_600_000, at=T0, effect_kind="trace")
    assert (result.status, result.debited_mc, result.debt_mc) == (
        "applied",
        5_600_000,
        0,
    )
    debits = [e for e in book.writes.entries if e["entry_type"] == "debit"]
    assert [(e["lot_id"], e["amount_mc"]) for e in debits] == [
        (monthly, 5_000_000),
        (order[1], 500_000),
        (order[2], 100_000),
    ]
    assert all(e["lot_id"] != expired for e in debits)


def test_a_shortfall_becomes_consumption_debt_linked_to_its_entry() -> None:
    book = _book()
    lot_id = _topup(book, grant=1_000)
    result = book.debit("trace:1", 3_000, at=T0, effect_kind="trace")
    assert (result.debited_mc, result.debt_mc) == (1_000, 2_000)
    assert book.lot(lot_id).balance_mc == 0
    assert book.open_debt_mc == 2_000
    (debt,) = book.writes.debts
    assert debt["debt_source_kind"] == "consumption"
    assert debt["original_grant_lot_id"] is None
    (origin,) = book.writes.origins
    debt_entry = next(e for e in book.writes.entries if e["entry_type"] == "debt")
    assert origin["consumption_entry_id"] == debt_entry["id"]
    assert book.available_trace_mc(T0) == 0


def test_new_capacity_settles_open_debt_oldest_first() -> None:
    book = _book()
    book.debit("trace:1", 3_000, at=T0, effect_kind="trace")
    book.debit("trace:2", 2_000, at=T0 + timedelta(seconds=1), effect_kind="trace")
    lot_id = _topup(book, grant=4_000, at=T0 + timedelta(minutes=1))
    first, second = book.debts
    settled = [(s["debt_id"], s["amount_mc"]) for s in book.writes.settlements]
    assert settled == [(first.id, 3_000), (second.id, 1_000)]
    assert all(s["settling_grant_lot_id"] == lot_id for s in book.writes.settlements)
    assert book.open_debt_mc == 1_000
    assert book.lot(lot_id).balance_mc == 0


def test_replayed_debit_changes_nothing() -> None:
    book = _book(known_effects={"trace:1"})
    _topup(book)
    before = len(book.writes.entries)
    assert book.debit("trace:1", 1_000, at=T0, effect_kind="trace").status == (
        "duplicate"
    )
    assert len(book.writes.entries) == before


@pytest.mark.parametrize(
    ("hold", "status"),
    [
        (SpendHold(quarantined=True, enforced=False), "applied"),
        (SpendHold(quarantined=False, enforced=True), "applied"),
        (SpendHold(quarantined=True, enforced=True), "credit_pending"),
    ],
)
def test_the_quarantine_hold_acts_only_after_the_enforcement(
    hold: SpendHold, status: str
) -> None:
    book = _book(spend_hold=hold)
    _topup(book)
    entries = len(book.writes.entries)
    result = book.debit("trace:1", 1_000, at=T0, effect_kind="trace")
    assert result.status == status
    if status == "credit_pending":
        assert len(book.writes.entries) == entries
        assert "trace:1" not in book.writes.effect_ids
    # Grants keep applying under the hold.
    _topup(book, order_id=uuid4())


def test_the_sweep_expires_at_the_deadline_once() -> None:
    book = _book()
    lot_id = _topup(book, grant=1_000)
    book.debit("trace:1", 400, at=T0, effect_kind="trace")
    deadline = book.lot(lot_id).expires_at
    book.sweep_expired(deadline - timedelta(microseconds=1))
    assert book.lot(lot_id).available_mc(deadline - timedelta(microseconds=1)) == 600
    book.sweep_expired(deadline + timedelta(days=1))  # a writer after the deadline
    (expiry,) = [e for e in book.writes.entries if e["entry_type"] == "expiry"]
    assert (expiry["amount_mc"], expiry["occurred_at"]) == (600, deadline)
    assert expiry["effect_id"] == expiry_effect_id(lot_id)
    book.sweep_expired(deadline + timedelta(days=1))
    assert sum(e["entry_type"] == "expiry" for e in book.writes.entries) == 1
    assert book.lot(lot_id).balance_mc == 0


def test_full_reversal_without_consumption_is_applied_once() -> None:
    book = _book()
    lot_id = _monthly(book)
    application = _targets(book, lot_id, 5_000_000)
    assert book.lot(lot_id).balance_mc == 0 and book.open_debt_mc == 0
    replay = _targets(book, lot_id, 5_000_000)
    older = _targets(book, lot_id, 2_500_000)
    assert application.reversal_delta_mc == 5_000_000  # type: ignore[attr-defined]
    assert replay.reversal_delta_mc == 0  # type: ignore[attr-defined]
    assert older.reversal_regressed  # type: ignore[attr-defined]
    assert sum(e["entry_type"] == "reversal" for e in book.writes.entries) == 1


def test_reversal_withdraws_available_then_turns_consumption_into_debt() -> None:
    book = _book()
    lot_id = _topup(book, grant=1_000_000)
    book.debit("trace:1", 600_000, at=T0, effect_kind="trace")
    _targets(book, lot_id, 500_000)  # 50%: 400k available, 100k owed
    assert book.lot(lot_id).balance_mc == 0
    assert book.open_debt_mc == 100_000
    (debt,) = [d for d in book.debts if d.source_kind == "reversal"]
    assert debt.original_lot_id == lot_id and debt.amount_mc == 100_000
    _targets(book, lot_id, 1_000_000, rev=3)  # full: the rest of the 600k used
    assert book.open_debt_mc == 600_000
    assert len(book.writes.origin_requests) == 2


def test_reversal_debt_is_settled_by_other_capacity_and_restored_back() -> None:
    book = _book()
    plan = _monthly(book, grant=1_000)
    topup = _topup(book, grant=5_000)
    book.debit("trace:1", 1_000, at=T0, effect_kind="trace")  # all of the plan
    _targets(book, plan, 1_000)  # chargeback: 1k consumed -> debt, paid by topup
    assert book.open_debt_mc == 0
    assert book.lot(topup).balance_mc == 4_000
    (settlement,) = book.writes.settlements
    assert settlement["settling_grant_lot_id"] == topup

    _targets(book, plan, 1_000, 1_000, rev=3)  # chargeback won
    restored = _by_effect(book, "restoration:")
    assert restored == Counter({"settlement_release": 1_000})
    assert book.lot(topup).balance_mc == 5_000
    assert book.lot(plan).balance_mc == 0  # the consumption stays consumed


def test_restoration_releases_open_debt_first_then_regrants() -> None:
    book = _book()
    lot_id = _topup(book, grant=1_000_000)
    book.debit("trace:1", 600_000, at=T0, effect_kind="trace")
    _targets(book, lot_id, 1_000_000)  # 400k withdrawn, 600k owed
    assert book.open_debt_mc == 600_000
    _targets(book, lot_id, 1_000_000, 700_000, rev=3)
    assert _by_effect(book, "restoration:") == Counter(
        {"debt_release": 600_000, "lot_regrant": 100_000}
    )
    assert book.open_debt_mc == 0
    assert book.lot(lot_id).balance_mc == 100_000
    _targets(book, lot_id, 1_000_000, 1_000_000, rev=4)
    assert book.lot(lot_id).balance_mc == 400_000  # back to before the reversal
    assert book.applied_restoration_mc(lot_id) == 1_000_000


def test_restoration_into_an_expired_lot_records_regrant_and_expiry() -> None:
    book = _book()
    lot_id = _topup(book, grant=1_000)
    _targets(book, lot_id, 1_000)
    later = book.lot(lot_id).expires_at + timedelta(days=1)
    book.apply_line_targets(
        lot_id,
        line_id="topup",
        payment_id="pay",
        financial_revision=3,
        reversal_target_mc=1_000,
        restoration_target_mc=1_000,
        at=later,
    )
    regrants = [e for e in book.writes.entries if e["entry_type"] == "lot_regrant"]
    expiries = [
        e
        for e in book.writes.entries
        if e["entry_type"] == "expiry" and str(e["effect_id"]).startswith("restor")
    ]
    assert [e["amount_mc"] for e in regrants] == [1_000]
    assert [e["amount_mc"] for e in expiries] == [1_000]
    assert book.lot(lot_id).balance_mc == 0
    assert book.available_mc(later) == 0


def test_reversal_of_an_expired_lot_only_owes_what_was_consumed() -> None:
    book = _book()
    lot_id = _topup(book, grant=1_000)
    book.debit("trace:1", 300, at=T0, effect_kind="trace")
    later = book.lot(lot_id).expires_at + timedelta(days=1)
    book.sweep_expired(later)
    book.apply_line_targets(
        lot_id,
        line_id="topup",
        payment_id="pay",
        financial_revision=2,
        reversal_target_mc=1_000,
        restoration_target_mc=0,
        at=later,
    )
    assert book.open_debt_mc == 300  # the 700 that expired unused is absorbed
    assert sum(e["entry_type"] == "reversal" for e in book.writes.entries) == 0


def test_restoration_never_exceeds_the_reversal() -> None:
    book = _book()
    lot_id = _topup(book, grant=1_000)
    application = _targets(book, lot_id, 300, 900)
    assert application.restoration_capped  # type: ignore[attr-defined]
    assert book.applied_restoration_mc(lot_id) == 300
    assert book.lot(lot_id).balance_mc == 1_000


def test_open_debt_rows_must_match_the_ledger() -> None:
    with pytest.raises(LedgerInvariantError):
        _book(ledger_debt_mc=5)


def _wedge_setup() -> tuple[LedgerBook, UUID, UUID, datetime]:
    """The reviewer's scenario: a line whose reversal debt was paid by a lot
    Q that another writer then expired (clock past Q's deadline)."""

    book = _book()
    q_paid = date(2026, 4, 26)
    deadline = topup_lot_expires_at(q_paid)
    t0 = deadline - timedelta(days=1)
    _topup(book, grant=1_000_000, paid_at=q_paid, at=t0, order_id=UUID(int=1))
    line = _topup(book, grant=500_000, paid_at=date(2026, 10, 1), at=t0)
    book.debit("trace:1", 1_500_000, at=t0, effect_kind="trace")
    book.apply_line_targets(
        line,
        line_id="topup",
        payment_id="pay-line",
        financial_revision=2,
        reversal_target_mc=500_000,
        restoration_target_mc=0,
        at=t0,
    )
    assert book.open_debt_mc == 500_000
    q = _topup(book, grant=600_000, paid_at=q_paid, at=t0, order_id=UUID(int=3))
    assert book.open_debt_mc == 0 and book.lot(q).balance_mc == 100_000
    book.sweep_expired(deadline + timedelta(seconds=2))  # writer B, clock ahead
    assert book.has_effect(expiry_effect_id(q))
    return book, line, q, deadline


def _win(book: LedgerBook, line: UUID, at: datetime) -> None:
    book.apply_line_targets(
        line,
        line_id="topup",
        payment_id="pay-line",
        financial_revision=3,
        reversal_target_mc=500_000,
        restoration_target_mc=500_000,
        at=at,
    )


def _assert_expired_lots_are_empty(book: LedgerBook, at: datetime) -> None:
    for lot in book.lots:
        if lot.expired(at):
            assert lot.balance_mc == 0, lot


def test_a_lagging_clock_acts_at_the_ledger_clock_and_cannot_wedge() -> None:
    book, line, q, deadline = _wedge_setup()
    ahead = deadline + timedelta(seconds=2)
    _win(book, line, deadline - timedelta(seconds=2))  # writer A, clock behind
    assert book.clock == ahead
    restored = _of_effects(book, "restoration:")
    assert {e["occurred_at"] for e in restored} == {ahead}
    assert Counter((e["entry_type"], e["lot_id"]) for e in restored) == Counter(
        {("settlement_release", q): 1, ("expiry", q): 1}
    )
    assert book.lot(q).balance_mc == 0
    later = deadline + timedelta(minutes=5)
    book.sweep_expired(later)  # used to raise LedgerInvariantError forever
    _assert_expired_lots_are_empty(book, later)


def test_a_restoration_never_refills_a_lot_whose_expiry_is_recorded() -> None:
    book, line, q, deadline = _wedge_setup()
    book._clock = None  # a book loaded without its clock: the second guard
    _win(book, line, deadline - timedelta(seconds=2))
    assert book.lot(q).balance_mc == 0
    book.sweep_expired(deadline + timedelta(minutes=5))


def test_the_book_clock_only_moves_forward() -> None:
    book = _book(clock_floor=T0)
    lot_id = _topup(book, at=T0 - timedelta(days=3))
    (grant,) = book.writes.entries
    assert grant["occurred_at"] == T0 == book.clock
    assert book.available_mc(T0 - timedelta(days=400)) == 500_000
    later = T0 + timedelta(hours=1)
    book.debit("trace:1", 1, at=later, effect_kind="trace")
    book.debit("trace:2", 1, at=T0, effect_kind="trace")
    assert [e["occurred_at"] for e in book.writes.entries[1:]] == [later, later]
    assert book.lot(lot_id).balance_mc == 499_998


def test_the_largest_applied_target_wins_when_a_book_is_loaded() -> None:
    lot_id = uuid4()
    effects = [
        reversal_effect_id(
            lot_id, line_id="topup", payment_id="pay", financial_revision=r, target_mc=t
        )
        for r, t in ((3, 600_000), (2, 300_000))
    ] + [
        restoration_effect_id(
            lot_id, line_id="topup", payment_id="pay", financial_revision=r, target_mc=t
        )
        for r, t in ((5, 50_000), (4, 100_000))
    ]
    for known in (effects, list(reversed(effects))):
        lot = LotState(
            id=lot_id,
            lot_kind="topup",
            paid_at=date(2026, 10, 1),
            expires_at=topup_lot_expires_at(date(2026, 10, 1)),
            granted_mc=1_000_000,
            grant_effect_id="topup:x:grant",
            sums=Counter(
                {"grant": 1_000_000, "reversal": 600_000, "lot_regrant": 100_000}
            ),
        )
        book = _book(lots=[lot], known_effects=known)
        assert book.applied_reversal_mc(lot_id) == 600_000
        assert book.applied_restoration_mc(lot_id) == 100_000
        unchanged = book.apply_line_targets(
            lot_id,
            line_id="topup",
            payment_id="pay",
            financial_revision=6,
            reversal_target_mc=600_000,
            restoration_target_mc=100_000,
            at=T0,
        )
        assert unchanged.reversal_delta_mc == unchanged.restoration_delta_mc == 0
        assert book.writes.entries == []
        assert book.lot(lot_id).balance_mc == 500_000


def test_a_settled_reversal_debt_returns_to_the_latest_expiring_payer_first() -> None:
    book = _book()
    plan = _monthly(book, grant=1_000)
    early = _topup(book, grant=500, paid_at=date(2026, 9, 1))
    late = _topup(book, grant=500, paid_at=date(2026, 9, 15))
    book.debit("trace:1", 1_000, at=T0, effect_kind="trace")  # the plan only
    _targets(book, plan, 1_000)  # chargeback: 1,000 owed, paid early then late
    assert [
        (s["settling_grant_lot_id"], s["amount_mc"]) for s in book.writes.settlements
    ] == [
        (early, 500),
        (late, 500),
    ]
    _targets(book, plan, 1_000, 600, rev=3)
    released = [
        (e["lot_id"], e["amount_mc"])
        for e in book.writes.entries
        if e["entry_type"] == "settlement_release"
    ]
    assert released == [(late, 500), (early, 100)]


def test_the_sweep_s_expiries_are_not_the_operation_s_effects() -> None:
    book = _book()
    old_order, new_order = uuid4(), uuid4()
    old = _topup(book, paid_at=date(2026, 10, 1), order_id=old_order)
    late = book.lot(old).expires_at + timedelta(days=1)
    book.sweep_expired(late)
    _topup(book, paid_at=late.date(), at=late, order_id=new_order)
    assert book.writes.swept_effect_ids == [expiry_effect_id(old)]
    assert book.writes.operation_effect_ids == (
        topup_grant_effect_id(old_order),
        topup_grant_effect_id(new_order),
    )
    assert set(book.writes.effect_ids) == {
        *book.writes.operation_effect_ids,
        expiry_effect_id(old),
    }


def _line_conservation(book: LedgerBook) -> None:
    """Per line: reversal debt in force never exceeds what the line consumed,
    and every release effect withdrew exactly what its debts' ``return:``
    effects released."""

    for lot in book.lots:
        assert book.reversal_debt_net_mc(lot.id) <= lot.consumed_mc, lot
    for effect in book.writes.effect_ids:
        if effect.startswith("release:"):
            moved = Counter()
            for entry in _of_effects(book, effect):
                moved[str(entry["entry_type"])] += int(entry["amount_mc"])
            assert moved["reversal"] == (
                moved["debt_release"] + moved["settlement_release"]
            ), effect


def _cascade_setup(payer_is_monthly: bool) -> tuple[LedgerBook, UUID, UUID, UUID]:
    """A's debt paid by B; B charged back; B's debt paid by C (a top-up) or
    by the monthly allowance (the reviewer's D1 and D2)."""

    book = _book()
    a = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1), order_id=UUID(int=1))
    book.debit("trace:1", 1_000_000, at=T0, effect_kind="trace")
    _targets(book, a, 1_000_000)  # A charged back: 1,000 credits owed
    b_paid = date(2026, 4, 27) if not payer_is_monthly else date(2026, 10, 2)
    b = _topup(book, grant=1_000_000, paid_at=b_paid, order_id=UUID(int=2))
    _targets(book, b, 1_000_000)  # B (which paid A's debt) charged back
    if payer_is_monthly:
        c = _monthly(book, grant=1_000_000)
    else:
        c = _topup(
            book, grant=1_000_000, paid_at=date(2026, 10, 3), order_id=UUID(int=3)
        )
    assert book.open_debt_mc == 0 and book.lot(c).balance_mc == 0
    _targets(book, a, 1_000_000, 1_000_000, rev=3)  # A's chargeback won
    return book, a, b, c


def test_a_won_line_returns_capacity_to_the_lot_that_really_paid() -> None:
    book, a, b, c = _cascade_setup(payer_is_monthly=False)
    assert (book.lot(a).balance_mc, book.lot(b).balance_mc) == (0, 0)
    assert book.lot(c).balance_mc == 1_000_000  # C paid B's debt, now cancelled
    assert book.open_debt_mc == 0
    assert book.reversal_debt_net_mc(b) == 0
    _line_conservation(book)
    restored = _by_effect(book, "restoration:")
    assert restored == Counter({"settlement_release": 1_000_000})  # = the delta
    after_b = book.lot(b).expires_at + timedelta(days=1)  # B ends 2026-10-27
    book.sweep_expired(after_b)
    assert book.available_mc(after_b) - book.open_debt_mc == 1_000_000  # D1


def test_a_cascade_into_the_monthly_allowance_does_not_roll_it_over() -> None:
    book, _a, b, monthly = _cascade_setup(payer_is_monthly=True)
    assert book.lot(b).balance_mc == 0
    assert book.lot(monthly).balance_mc == 1_000_000
    _line_conservation(book)
    after_cycle = book.lot(monthly).expires_at + timedelta(days=1)
    book.sweep_expired(after_cycle)
    assert book.available_mc(after_cycle) - book.open_debt_mc == 0  # D2


def test_a_partly_charged_back_payer_keeps_what_its_funding_still_covers() -> None:
    book = _book()
    a = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1), order_id=UUID(int=1))
    book.debit("trace:1", 1_000_000, at=T0, effect_kind="trace")
    _targets(book, a, 1_000_000)
    b = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 2), order_id=UUID(int=2))
    _targets(book, b, 500_000)  # B half refunded: 500 owed, nobody to pay it
    assert book.open_debt_mc == 500_000
    _targets(book, a, 1_000_000, 1_000_000, rev=3)
    # B's payment of A's debt is undone: B's debt is cancelled (released, it
    # was open) and B keeps the half of its funding the refund left.
    assert book.open_debt_mc == 0
    assert book.lot(b).balance_mc == 500_000
    _line_conservation(book)


def _c8(
    variant: int, with_chargeback: bool, q_monthly: bool
) -> tuple[LedgerBook, dict[str, UUID]]:
    """Payer P (2,000) pays A's debt, is half refunded, Q pays P's debt; A's
    chargeback is won -- or A never was charged back (the reviewer's C8)."""

    book = _book()
    if variant == 1:  # P pays A's debt, then its own debit
        a = _topup(
            book, grant=1_000_000, paid_at=date(2026, 4, 25), order_id=UUID(int=1)
        )
        book.debit("trace:a", 1_000_000, at=T0, effect_kind="trace")
        if with_chargeback:
            _targets(book, a, 1_000_000)
        p = _topup(
            book, grant=2_000_000, paid_at=date(2026, 4, 30), order_id=UUID(int=2)
        )
        book.debit("trace:p", 1_000_000, at=T0, effect_kind="trace")
    else:  # P's own debit, then P pays A's debt
        p = _topup(
            book, grant=2_000_000, paid_at=date(2026, 4, 30), order_id=UUID(int=2)
        )
        book.debit("trace:p", 1_000_000, at=T0, effect_kind="trace")
        a = _topup(
            book, grant=1_000_000, paid_at=date(2026, 4, 25), order_id=UUID(int=1)
        )
        book.debit("trace:a", 1_000_000, at=T0, effect_kind="trace")
        if with_chargeback:
            _targets(book, a, 1_000_000)
    _targets(book, p, 1_000_000)  # P half refunded
    if q_monthly:
        q = _monthly(book, grant=1_000_000)
    else:
        q = _topup(
            book, grant=1_000_000, paid_at=date(2026, 10, 20), order_id=UUID(int=3)
        )
    if with_chargeback:
        _targets(book, a, 1_000_000, 1_000_000, rev=3)  # A's chargeback won
    return book, {"A": a, "P": p, "Q": q}


@pytest.mark.parametrize("q_monthly", [False, True], ids=["topup-Q", "monthly-Q"])
@pytest.mark.parametrize("variant", [1, 2])
def test_a_won_chargeback_costs_nothing_when_the_payer_was_charged_back(
    variant: int, q_monthly: bool
) -> None:
    outcomes = []
    for with_chargeback in (True, False):
        book, lots = _c8(variant, with_chargeback, q_monthly)
        _line_conservation(book)
        for lot in book.lots:  # no line keeps capacity while it owes reversal
            if lot.available_mc(T0) > 0:
                assert book.reversal_debt_net_mc(lot.id) == 0
        balances = {name: book.lot(lot).balance_mc for name, lot in lots.items()}
        later = max(book.lot(lots["P"]).expires_at, book.lot(lots["Q"]).expires_at)
        book.sweep_expired(later + timedelta(days=1))
        outcomes.append(
            (balances, book.available_mc(later + timedelta(days=1)) - book.open_debt_mc)
        )
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == {"A": 0, "P": 0, "Q": 1_000_000}


def test_two_won_lines_cascade_through_one_payer_under_distinct_effects() -> None:
    book = _book()
    a1 = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1), order_id=UUID(int=1))
    a2 = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 2), order_id=UUID(int=2))
    book.debit("trace:1", 2_000_000, at=T0, effect_kind="trace")
    _targets(book, a1, 1_000_000)
    _targets(book, a2, 1_000_000)
    payer = _topup(
        book, grant=2_000_000, paid_at=date(2026, 10, 3), order_id=UUID(int=3)
    )
    _targets(book, payer, 2_000_000)  # the payer of both debts charged back
    q = _topup(book, grant=2_000_000, paid_at=date(2026, 10, 4), order_id=UUID(int=4))
    _targets(book, a1, 1_000_000, 1_000_000, rev=3)
    _targets(book, a2, 1_000_000, 1_000_000, rev=4)
    releases = [e for e in book.writes.effect_ids if e.startswith("release:")]
    assert len(releases) == len(set(releases)) == 2
    assert all(e.startswith(f"release:{payer}:") for e in releases)
    # Each release id digests the ``return:`` effect (A1's or A2's debt,
    # released by its restoration) that paid the payer back: stable across
    # replays, distinct per trigger.
    debts = {d.original_lot_id: d.id for d in book.debts if d.source_kind == "reversal"}
    restorations = {
        e.split(":")[1]: e
        for e in book.writes.effect_ids
        if e.startswith("restoration:")
    }
    returns = [
        f"return:{debts[line]}:{_sha(restorations[str(line)])}" for line in (a1, a2)
    ]
    assert set(returns) <= set(book.writes.effect_ids)
    assert sorted(e.rpartition(":")[2] for e in releases) == sorted(
        _sha(e) for e in returns
    )
    assert book.lot(q).balance_mc == 2_000_000 and book.lot(payer).balance_mc == 0
    _line_conservation(book)


def test_a_release_effect_recorded_twice_is_an_invariant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two releases of one payer under one id would merge their entries."""

    monkeypatch.setattr(
        ledger_module, "release_effect_id", lambda payer, _trigger: f"release:{payer}:x"
    )
    with pytest.raises(LedgerInvariantError, match="release effect"):
        test_two_won_lines_cascade_through_one_payer_under_distinct_effects()


def _apply(
    book: LedgerBook, lot: UUID, reversal: int, restoration: int, rev: int, at: datetime
) -> None:
    book.apply_line_targets(
        lot,
        line_id="line",
        payment_id="pay",
        financial_revision=rev,
        reversal_target_mc=reversal,
        restoration_target_mc=restoration,
        at=at,
    )


def _lots_and_debt(book: LedgerBook, lots: dict[str, UUID]) -> tuple[Any, ...]:
    return (
        {name: book.lot(lot).balance_mc for name, lot in lots.items()},
        book.open_debt_mc,
    )


def test_a_reversal_won_at_once_moves_no_capacity_between_lots() -> None:
    """The reviewer's R4-1: X already owes a debt paid by top-up P1; a further
    25% paid by the monthly allowance and won at once must go back to the
    allowance, not to P1 (which outlives it by months)."""

    at = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    outcomes = []
    for with_pair in (True, False):
        book = _book()
        x = _topup(book, grant=1_000_000, paid_at=date(2026, 5, 1), at=at)
        p1 = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1), at=at)
        book.debit("trace:1", 1_000_000, at=at, effect_kind="trace")  # X consumed
        _apply(book, x, 500_000, 0, 2, at)  # D1 = 500, paid by P1
        m = _monthly(book, grant=500_000, at=at)
        if with_pair:
            _apply(book, x, 750_000, 0, 3, at)  # D2 = 250, paid by the allowance
            _apply(book, x, 750_000, 250_000, 4, at)  # ... won at once
            _line_conservation(book)
        outcomes.append(_lots_and_debt(book, {"X": x, "P1": p1, "M": m}))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0] == ({"X": 0, "P1": 500_000, "M": 500_000}, 0)


def test_a_reversal_won_at_once_never_goes_to_an_expired_payer() -> None:
    """The reviewer's R4-1b: the 250 the allowance paid must not go back to
    the expired top-up P1 (and expire again) -- the customer lost 250."""

    t0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    usable = []
    for with_pair in (True, False):
        book = _book()
        x = _topup(book, grant=1_000_000, paid_at=date(2026, 9, 1), at=t0)
        book.debit("trace:1", 1_000_000, at=t0, effect_kind="trace")
        p1 = _topup(book, grant=1_000_000, paid_at=date(2026, 4, 20), at=t0)
        _apply(book, x, 500_000, 0, 2, t0)  # D1 = 500, paid by P1 (ends 10-20)
        book.sweep_expired(t1)  # P1's other 500 expire
        m = _monthly(book, grant=500_000, at=t1)
        if with_pair:
            _apply(book, x, 750_000, 0, 3, t1)  # D2 = 250, paid by M
            _apply(book, x, 750_000, 250_000, 4, t1)  # won at once
        usable.append((book.available_mc(t1), _lots_and_debt(book, {"P1": p1, "M": m})))
    assert (
        usable[0]
        == usable[1]
        == (
            500_000,
            ({"P1": 0, "M": 500_000}, 0),
        )
    )


def test_a_reversal_won_at_once_after_a_partial_win_moves_nothing() -> None:
    """Newest debt first must also hold after a partial win returned part of
    an older debt to the same payer: allocating the line's returns to the
    newest debts would pay Q back with R's money."""

    at = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    outcomes = []
    for with_pair in (True, False):
        book = _book()
        line = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1), at=at)
        book.debit("trace:1", 1_000_000, at=at, effect_kind="trace")
        r = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 5), at=at)
        _apply(book, line, 500_000, 0, 2, at)  # D1 = 500, paid by R
        _apply(book, line, 500_000, 200_000, 3, at)  # 200 of it back to R
        q = _topup(book, grant=250_000, paid_at=date(2026, 10, 2), at=at)
        _apply(book, line, 750_000, 200_000, 4, at)  # D3 = 250, paid by Q
        if with_pair:
            _apply(book, line, 850_000, 200_000, 5, at)  # D5 = 100, paid by R
            _apply(book, line, 850_000, 300_000, 6, at)  # ... won at once
            _line_conservation(book)
        outcomes.append(_lots_and_debt(book, {"L": line, "R": r, "Q": q}))
    assert outcomes[0] == outcomes[1]
    assert outcomes[0] == ({"L": 0, "R": 700_000, "Q": 0}, 0)


def test_same_instant_debts_of_a_line_settle_in_revision_order() -> None:
    """Revision 10 is newer than revision 9 even though its effect id sorts
    first as text: the older debt is settled first, and winning revision
    10's part back returns what paid THAT debt."""

    book = _book()
    line = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1))
    book.debit("trace:1", 1_000_000, at=T0, effect_kind="trace")
    _apply(book, line, 300_000, 0, 9, T0)  # D9 = 300, open
    _apply(book, line, 500_000, 0, 10, T0)  # D10 = 200, open (same instant)
    p = _topup(book, grant=300_000, paid_at=date(2026, 10, 2))
    q = _topup(book, grant=200_000, paid_at=date(2026, 10, 3))
    by_debt = {d.amount_mc: dict(d.settled_by_lot) for d in book.debts}
    assert by_debt == {300_000: {p: 300_000}, 200_000: {q: 200_000}}
    _apply(book, line, 500_000, 200_000, 11, T0)  # revision 10's part won
    assert (book.lot(p).balance_mc, book.lot(q).balance_mc) == (0, 200_000)


def test_a_won_line_cascades_through_an_expired_charged_back_payer() -> None:
    """P paid A's debt, was charged back (Q paid P's debt) and expired; A's
    win must still cancel P's debt and give Q its money back -- as if A had
    never been charged back."""

    after = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    outcomes = []
    for with_chargeback in (True, False):
        book = _book()
        a = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1))
        book.debit("trace:1", 1_000_000, at=T0, effect_kind="trace")
        if with_chargeback:
            _apply(book, a, 1_000_000, 0, 2, T0)  # owed, open
        p = _topup(book, grant=1_000_000, paid_at=date(2026, 4, 25))  # ends 10-25
        _apply(book, p, 1_000_000, 0, 2, T0)  # P charged back
        q = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 2))
        if with_chargeback:
            _apply(book, a, 1_000_000, 1_000_000, 3, after)  # P has expired
        book.sweep_expired(after)
        _line_conservation(book)
        outcomes.append(
            (book.lot(q).balance_mc, book.available_mc(after) - book.open_debt_mc)
        )
    assert outcomes[0] == outcomes[1] == (1_000_000, 1_000_000)


def test_each_released_debt_hangs_from_its_own_return_effect() -> None:
    book = _book()
    line = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1))
    book.debit("trace:1", 1_000_000, at=T0, effect_kind="trace")
    payer = _topup(book, grant=2_000_000, paid_at=date(2026, 10, 2))
    _targets(book, line, 400_000)  # D1
    _targets(book, line, 1_000_000, rev=3)  # D2
    _targets(book, line, 1_000_000, 1_000_000, rev=4)  # both released
    (restoration,) = [e for e in book.writes.effect_ids if e.startswith("restor")]
    d1, d2 = sorted(book.debts, key=lambda debt: debt.amount_mc)
    returns = {
        str(e["effect_id"]): (e["entry_type"], e["lot_id"], e["amount_mc"])
        for e in book.writes.entries
        if str(e["effect_id"]).startswith("return:")
    }
    assert returns == {
        f"return:{d1.id}:{_sha(restoration)}": ("settlement_release", payer, 400_000),
        f"return:{d2.id}:{_sha(restoration)}": ("settlement_release", payer, 600_000),
    }
    assert not [e for e in book.writes.entries if e["effect_id"] == restoration]
    assert book.lot(payer).balance_mc == 2_000_000


def test_a_debt_released_twice_by_one_effect_is_an_invariant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ledger_module, "return_effect_id", lambda debt, _trigger: f"return:{debt}:x"
    )
    book = _book()
    line = _topup(book, grant=1_000_000, paid_at=date(2026, 10, 1))
    book.debit("trace:1", 1_000_000, at=T0, effect_kind="trace")
    _topup(book, grant=1_000_000, paid_at=date(2026, 10, 2))
    _targets(book, line, 1_000_000)
    _targets(book, line, 1_000_000, 300_000, rev=3)
    with pytest.raises(LedgerInvariantError, match="released twice"):
        _targets(book, line, 1_000_000, 600_000, rev=4)


def test_equal_expiries_and_equal_instants_break_ties_deterministically() -> None:
    paid = date(2026, 5, 1)  # both expire 2026-11-01
    later = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
    outcomes = set()
    for _ in range(40):
        book = _book()
        x = _topup(book, grant=1_000_000, paid_at=paid, order_id=UUID(int=1))
        y = _topup(book, grant=1_000_000, paid_at=paid, order_id=UUID(int=2))
        book.debit("trace:1", 1_000_000, at=T0, effect_kind="trace")
        consumed = (book.lot(x).consumed_mc, book.lot(y).consumed_mc)
        book.sweep_expired(later)
        book.apply_line_targets(
            x,
            line_id="topup",
            payment_id="p1",
            financial_revision=2,
            reversal_target_mc=1_000_000,
            restoration_target_mc=0,
            at=later,
        )
        lagging = _book()
        line = _topup(lagging, grant=500_000, paid_at=date(2026, 10, 1))
        lagging.debit("trace:1", 500_000, at=T0, effect_kind="trace")
        lagging.debit("trace:2", 300_000, at=T0, effect_kind="trace")
        _targets(lagging, line, 500_000, rev=0)  # clamped to T0: equal instants
        outcomes.add(
            (consumed, book.open_debt_mc, tuple(d.source_kind for d in lagging.debts))
        )
    # The lot named by the smaller grant effect id goes first, every time.
    assert outcomes == {((1_000_000, 0), 1_000_000, ("consumption", "reversal"))}


def test_equal_debts_of_two_lines_at_one_instant_settle_by_line() -> None:
    """Two lines owing equal targets at the same instant: the debt of the
    line with the smaller grant effect id is settled first, every time --
    never by the random lot id their reversal effect ids embed."""

    first = set()
    for _ in range(40):
        book = _book()
        a = _topup(
            book, grant=1_000_000, paid_at=date(2026, 10, 1), order_id=UUID(int=1)
        )
        b = _topup(
            book, grant=1_000_000, paid_at=date(2026, 10, 2), order_id=UUID(int=2)
        )
        book.debit("trace:1", 2_000_000, at=T0, effect_kind="trace")
        _apply(book, a, 500_000, 0, 2, T0)
        _apply(book, b, 500_000, 0, 2, T0)  # same instant, same target
        payer = _topup(book, grant=500_000, paid_at=date(2026, 10, 3))
        first.update(
            book.lot(debt.original_lot_id).order_id
            for debt in book.debts
            if debt.settled_by_lot[payer] and debt.original_lot_id is not None
        )
    assert first == {UUID(int=1)}


def test_a_debit_reports_the_instant_it_acted_at() -> None:
    book = _book(clock_floor=T0)
    _topup(book)
    result = book.debit("trace:1", 1, at=T0 - timedelta(hours=2), effect_kind="trace")
    assert result.at == T0 == book.clock
    assert book.debit("trace:1", 1, at=T0, effect_kind="trace").at == T0


# ---------------------------------------------------------------------------
# SQLite round trip through the adapter.
# ---------------------------------------------------------------------------


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ledger.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    finally:
        await engine.dispose()


def _simulated(instant: datetime) -> Callable[[], Awaitable[datetime]]:
    """A reference clock for tests that simulate time past the real date."""

    async def clock() -> datetime:
        return instant

    return clock


def _ledger(session: AsyncSession, at: datetime) -> CreditLedger:
    """An adapter whose database clock reads the test's simulated ``at``."""

    return CreditLedger(session, reference_clock=_simulated(at))


async def _state(session: AsyncSession, builder_id: UUID, at: datetime) -> LedgerBook:
    return await _ledger(session, at).open_book(builder_id, at=at)


async def test_the_adapter_persists_and_reloads_the_same_book(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = uuid4()
    async with sessions() as session, session.begin():
        ledger = _ledger(session, T0)
        book = await ledger.open_book(builder_id, at=T0)
        plan = _monthly(book, grant=1_000_000)
        topup = _topup(book, grant=500_000)
        book.debit("trace:1", 1_200_000, at=T0, effect_kind="trace")
        book.apply_line_targets(
            plan,
            line_id="plan",
            payment_id="pay",
            financial_revision=2,
            reversal_target_mc=1_000_000,
            restoration_target_mc=0,
            at=T0 + timedelta(hours=1),
        )
        expected_lots = {lot.id: lot.balance_mc for lot in book.lots}
        expected_debt = book.open_debt_mc
        created = await ledger.persist(book)
        assert len(created) == len(set(created)) >= 4

    async with sessions() as session, session.begin():
        reloaded = await _state(session, builder_id, T0 + timedelta(hours=1))
        assert {lot.id: lot.balance_mc for lot in reloaded.lots} == expected_lots
        assert reloaded.open_debt_mc == expected_debt == 1_000_000 - 300_000
        assert reloaded.applied_reversal_mc(plan) == 1_000_000
        assert reloaded.lot(topup).balance_mc == 0
        entries = (
            await session.execute(
                select(
                    CreditLedgerEntry.entry_type,
                    CreditLedgerEntry.amount_mc,
                    CreditLedgerEntry.lot_id,
                ).where(CreditLedgerEntry.builder_id == builder_id)
            )
        ).all()
        fold = fold_ledger(LedgerMovement(t, int(a), l) for t, a, l in entries)
        derived = await CreditRepository(session).derived_balance(builder_id)
        assert derived == fold
        assert dict(fold.lots) == expected_lots and fold.debt_mc == expected_debt
        (reversal_debt,) = (
            await session.execute(
                select(CreditDebt).where(CreditDebt.debt_source_kind == "reversal")
            )
        ).scalars()
        origins = (
            await session.execute(
                select(func.count()).where(CreditDebtOrigin.debt_id == reversal_debt.id)
            )
        ).scalar_one()
        assert origins == 1  # the plan's debit entry
        assert (
            await session.execute(
                select(func.count()).select_from(CreditDebtSettlement)
            )
        ).scalar_one() == 1


async def test_the_adapter_debit_is_idempotent_and_sweeps(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = uuid4()
    async with sessions() as session, session.begin():
        ledger = _ledger(session, T0)
        book = await ledger.open_book(builder_id, at=T0)
        lot_id = _topup(book, grant=2_000)
        await ledger.persist(book)
    for _ in range(2):
        async with sessions() as session, session.begin():
            result = await _ledger(session, T0).debit(
                builder_id, "trace:x", 500, at=T0, effect_kind="trace"
            )
    assert result.status == "duplicate"
    deadline = topup_lot_expires_at(date(2026, 10, 12))
    async with sessions() as session, session.begin():
        ledger = _ledger(session, deadline)
        book = await ledger.open_book(builder_id, at=deadline)
        assert book.available_mc(deadline) == 0
        await ledger.persist(book)
    async with sessions() as session:
        expiry = (
            await session.execute(
                select(CreditLedgerEntry.amount_mc).where(
                    CreditLedgerEntry.entry_type == "expiry"
                )
            )
        ).scalar_one()
        assert expiry == 1_500
        kinds = Counter(
            (await session.execute(select(CreditEffect.effect_kind))).scalars()
        )
        assert kinds == Counter({"grant": 1, "trace": 1, "expiry": 1})
        assert (
            await session.execute(select(CreditLot.id).where(CreditLot.id == lot_id))
        ).scalar_one() == lot_id


async def test_the_spend_hold_reads_quarantined_rows_of_the_builder_only(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    held, other = uuid4(), uuid4()
    async with sessions() as session, session.begin():
        session.add(
            BillingTopupOrder(
                id=uuid4(),
                builder_id=held,
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
    async with sessions() as session, session.begin():
        ledger = _ledger(session, T0)
        assert await ledger.spend_hold(held) == SpendHold(True, False)
        assert await ledger.spend_hold(other) == SpendHold(False, False)
        session.add(
            CreditEnforcementState(
                id=1, credit_enforcement_at=T0, set_by="test", set_at=T0
            )
        )
        await session.flush()
        assert (await ledger.spend_hold(held)).active
        book = await ledger.open_book(held, at=T0)
        _topup(book)
        assert book.debit("t", 1, at=T0, effect_kind="trace").status == (
            "credit_pending"
        )


async def test_a_refused_or_replayed_debit_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = uuid4()
    async with sessions() as session, session.begin():
        ledger = _ledger(session, T0)
        book = await ledger.open_book(builder_id, at=T0)
        lot_id = _topup(book, grant=2_000)
        book.debit("trace:once", 1_000, at=T0, effect_kind="trace")
        await ledger.persist(book)
        session.add(
            BillingTopupOrder(
                id=uuid4(),
                builder_id=builder_id,
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
            CreditEnforcementState(
                id=1, credit_enforcement_at=T0, set_by="test", set_at=T0
            )
        )

    async def rows() -> Counter[str]:
        async with sessions() as session:
            return Counter(
                {
                    "entries": (
                        await session.execute(
                            select(func.count()).select_from(CreditLedgerEntry)
                        )
                    ).scalar_one(),
                    "effects": (
                        await session.execute(
                            select(func.count()).select_from(CreditEffect)
                        )
                    ).scalar_one(),
                }
            )

    before = await rows()
    past_deadline = topup_lot_expires_at(date(2026, 10, 12)) + timedelta(days=1)
    for effect_id, status in (
        ("trace:held", "credit_pending"),
        ("trace:once", "duplicate"),
    ):
        async with sessions() as session, session.begin():
            result = await _ledger(session, past_deadline).debit(
                builder_id, effect_id, 500, at=past_deadline, effect_kind="trace"
            )
        assert result.status == status
        assert await rows() == before  # not even the deadline sweep's expiry
    async with sessions() as session, session.begin():
        await session.execute(
            CreditEnforcementState.__table__.delete()  # type: ignore[attr-defined]
        )
    async with sessions() as session, session.begin():
        accepted = await _ledger(session, past_deadline).debit(
            builder_id, "trace:after", 500, at=past_deadline, effect_kind="trace"
        )
    assert (accepted.status, accepted.debt_mc) == ("applied", 500)
    async with sessions() as session:
        expiry = (
            await session.execute(
                select(CreditLedgerEntry.amount_mc).where(
                    CreditLedgerEntry.entry_type == "expiry",
                    CreditLedgerEntry.lot_id == lot_id,
                )
            )
        ).scalar_one()
    assert expiry == 1_000


def _as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes (stored in UTC)."""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class _Warnings:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


async def test_a_writer_ahead_of_the_database_clock_acts_at_the_tolerance(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings = _Warnings()
    monkeypatch.setattr(ledger_module, "logger", warnings)
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    clock = _simulated(now)
    limit = now + timedelta(minutes=5)
    builder_id = uuid4()
    utc_wall_read_as_sao_paulo = now.replace(tzinfo=None).replace(
        tzinfo=CREDIT_TIMEZONE
    )  # 3 hours AHEAD
    sao_paulo_wall_labelled_utc = now.astimezone(CREDIT_TIMEZONE).replace(
        tzinfo=timezone.utc
    )  # 3 hours behind
    async with sessions() as session, session.begin():
        ledger = CreditLedger(session, reference_clock=clock)
        book = await ledger.open_book(builder_id, at=now)
        _topup(book, at=now)
        await ledger.persist(book)
    for number, ahead in enumerate(
        (
            now + timedelta(days=200),
            utc_wall_read_as_sao_paulo,
            now + timedelta(minutes=6),
        )
    ):
        async with sessions() as session, session.begin():
            result = await CreditLedger(session, reference_clock=clock).debit(
                builder_id, f"trace:{number}", 1, at=ahead, effect_kind="trace"
            )
        assert (result.status, result.at) == ("applied", limit)
    assert len(warnings.events) == 3
    assert warnings.events[0][1]["seconds_ahead"] == 200 * 86_400
    assert warnings.events[0][1]["tolerance_seconds"] == 300
    async with sessions() as session:
        stamps = (
            await session.execute(
                select(CreditLedgerEntry.occurred_at).where(
                    CreditLedgerEntry.effect_id.like("trace:%")
                )
            )
        ).scalars()
        assert {_as_utc(stamp) for stamp in stamps} == {limit}
    async with sessions() as session, session.begin():
        book = await CreditLedger(session, reference_clock=clock).open_book(
            builder_id, at=sao_paulo_wall_labelled_utc
        )
        assert book.clock == limit  # behind: raised, then the ledger's clock
    async with sessions() as session, session.begin():
        wider = CreditLedger(
            session, reference_clock=clock, clock_tolerance=timedelta(minutes=10)
        )
        assert await wider.checked_instant(builder_id, now + timedelta(minutes=8)) == (
            now + timedelta(minutes=8)
        )
    assert len(warnings.events) == 3  # inside the tolerance: no warning
    for tolerance in (timedelta(0), timedelta(seconds=601)):
        with pytest.raises(ValueError):
            CreditLedger(AsyncSession(), clock_tolerance=tolerance)


async def test_a_ledger_clock_beyond_the_database_clock_refuses_until_it_catches_up(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The reviewer's r4_db_clock_back: the database clock steps back after a
    writer 4 minutes ahead (inside the tolerance) wrote; the builder is
    refused, writing nothing, until the database clock catches up."""

    database = [datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)]

    async def reference() -> datetime:
        return database[0]

    builder_id = uuid4()

    async def debit(number: int, at: datetime) -> DebitResult:
        async with sessions() as session, session.begin():
            ledger = CreditLedger(session, reference_clock=reference)
            return await ledger.debit(
                builder_id, f"trace:{number}", 1, at=at, effect_kind="trace"
            )

    first = await debit(1, database[0] + timedelta(minutes=4))
    assert first.at == database[0] + timedelta(minutes=4)
    database[0] -= timedelta(minutes=2)  # the database clock steps back
    async with sessions() as session:
        before = (
            await session.execute(select(func.count()).select_from(CreditLedgerEntry))
        ).scalar_one()
    with pytest.raises(LedgerClockAhead, match="ledger clock"):
        await debit(2, database[0])
    async with sessions() as session:
        after = (
            await session.execute(select(func.count()).select_from(CreditLedgerEntry))
        ).scalar_one()
    assert after == before
    database[0] += timedelta(minutes=1, seconds=1)  # caught up: works again
    assert (await debit(3, database[0])).at == first.at


async def test_a_ledger_clock_already_ahead_fails_closed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    builder_id = uuid4()
    poisoned = now + timedelta(days=200)
    async with sessions() as session, session.begin():  # an older, unguarded writer
        ledger = CreditLedger(session, reference_clock=_simulated(poisoned))
        book = await ledger.open_book(builder_id, at=poisoned)
        _topup(book, at=poisoned)
        await ledger.persist(book)
    async with sessions() as session, session.begin():
        with pytest.raises(LedgerClockAhead, match="ledger clock"):
            await CreditLedger(session, reference_clock=_simulated(now)).open_book(
                builder_id, at=now
            )


async def test_the_database_clock_is_the_default_reference(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session:
        database_now = await CreditRepository(session).database_now()
    real = datetime.now(timezone.utc)
    assert abs(database_now - real) < timedelta(minutes=1)
    async with sessions() as session, session.begin():
        book = await CreditLedger(session).open_book(
            uuid4(), at=real + timedelta(hours=1)
        )
    assert timedelta(minutes=4) < book.clock - real < timedelta(minutes=6)


def _spend(book: LedgerBook, amount: int) -> None:
    book.debit(f"trace:{amount}", amount, at=T0, effect_kind="trace")


async def test_a_reloaded_book_attributes_releases_like_the_continuous_one(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """The reviewer's m01: each step is its own transaction (one reload per
    fact, as in production); the second restoration must see the first."""

    builder_id = uuid4()
    ids: dict[str, UUID] = {}
    now = T0 + timedelta(hours=5)  # after every instant the steps act at

    async def step(operation: Callable[[LedgerBook], None]) -> LedgerBook:
        async with sessions() as session, session.begin():
            ledger = _ledger(session, now)
            book = await ledger.open_book(builder_id, at=now)
            operation(book)
            await ledger.persist(book)
        return book

    def grant(name: str, paid: date) -> Callable[[LedgerBook], None]:
        def operation(book: LedgerBook) -> None:
            # Random order ids: SQLite stores an all-digit UUID as a number.
            ids[name] = _topup(book, grant=1_000_000, paid_at=paid)

        return operation

    def targets(
        reversal: int, restoration: int, rev: int
    ) -> Callable[[LedgerBook], None]:
        def operation(book: LedgerBook) -> None:
            _targets(book, ids["L"], reversal, restoration, rev=rev)

        return operation

    await step(grant("L", date(2026, 10, 1)))
    await step(lambda book: _spend(book, 600_000))
    await step(targets(1_000_000, 0, 2))  # 400 withdrawn, 600 owed
    await step(grant("Q", date(2026, 10, 2)))  # Q pays the 600
    await step(targets(1_000_000, 500_000, 3))  # half won (one transaction)
    await step(targets(1_000_000, 1_000_000, 4))  # fully won (after a reload)
    async with sessions() as session, session.begin():
        book = await _ledger(session, now).open_book(builder_id, at=now)
    assert book.lot(ids["Q"]).balance_mc == 1_000_000  # never more than granted
    assert book.lot(ids["L"]).balance_mc == 400_000
    assert book.open_debt_mc == 0


async def test_the_database_clock_not_the_application_s_is_the_reference(
    sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    database = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)  # != the app's

    async def database_now(_self: CreditRepository) -> datetime:
        return database

    monkeypatch.setattr(CreditRepository, "database_now", database_now)
    application = datetime.now(timezone.utc)  # days ahead of that database
    async with sessions() as session, session.begin():
        book = await CreditLedger(session).open_book(uuid4(), at=application)
    assert book.clock == database + timedelta(minutes=5)  # limited, not the app's
    async with sessions() as session, session.begin():
        book = await CreditLedger(session).open_book(
            uuid4(), at=database - timedelta(days=3)
        )
    assert book.clock == database - timedelta(minutes=5)  # raised, not 3 days back


async def test_a_lagging_writer_is_raised_to_the_database_clock_minus_tolerance(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    database = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)
    sao_paulo_wall_labelled_utc = database.astimezone(CREDIT_TIMEZONE).replace(
        tzinfo=timezone.utc
    )  # 3 hours behind
    async with sessions() as session, session.begin():
        ledger = _ledger(session, database)
        instant = await ledger.checked_instant(uuid4(), sao_paulo_wall_labelled_utc)
        assert instant == database - timedelta(minutes=5)
        assert await ledger.checked_instant(
            uuid4(), database - timedelta(minutes=4)
        ) == (database - timedelta(minutes=4))
        book = await ledger.open_book(uuid4(), at=sao_paulo_wall_labelled_utc)
        result = book.debit(
            "trace:1", 1, at=sao_paulo_wall_labelled_utc, effect_kind="t"
        )
    assert result.at == database - timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Group 3a, O5: a monthly allowance is SPENT only from its cycle_start.
# ---------------------------------------------------------------------------

# The renewal of the T0 cycle (2026-10-12 -> 2026-11-12), paid at T0: early.
NEXT_START, NEXT_END = date(2026, 11, 12), date(2026, 12, 12)
NEXT_SPEND = start_of_civil_day(NEXT_START)  # 2026-11-12 03:00 UTC


def test_the_spend_predicate_opens_at_the_civil_start_of_cycle_start() -> None:
    book = _book()
    lot_id = _monthly(book, start=NEXT_START, end=NEXT_END, at=T0)
    lot = book.lot(lot_id)
    assert NEXT_SPEND == datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
    assert lot.spend_starts_at() == NEXT_SPEND
    before = NEXT_SPEND - timedelta(microseconds=1)
    # Held (what a reversal would withdraw) but not spendable before the start.
    assert (lot.available_mc(before), lot.spendable_mc(before)) == (5_000_000, 0)
    assert lot.spendable_mc(NEXT_SPEND) == lot.available_mc(NEXT_SPEND) == 5_000_000
    assert book.available_mc(before) == 0
    assert book.available_mc(NEXT_SPEND) == 5_000_000
    assert book.available_trace_mc(before) == 0
    top_id = _topup(book, grant=1_000)
    assert book.lot(top_id).spend_starts_at() is None
    assert book.lot(top_id).spendable_mc(T0) == book.lot(top_id).available_mc(T0)


def test_an_early_renewal_is_not_debited_before_its_cycle_start() -> None:
    book = _book()
    current = _monthly(book, grant=1_000_000)  # 2026-10-12 -> 2026-11-12
    renewal = _monthly(book, start=NEXT_START, end=NEXT_END, at=T0)
    first = book.debit("trace:1", 1_500_000, at=T0, effect_kind="trace")
    assert (first.debited_mc, first.debt_mc, first.at) == (1_000_000, 500_000, T0)
    assert book.lot(renewal).balance_mc == 5_000_000
    assert book.lot(current).balance_mc == 0
    debited = {e["lot_id"] for e in book.writes.entries if e["entry_type"] == "debit"}
    assert debited == {current}
    # Not even one microsecond early: the debit's own instant decides.
    almost = NEXT_SPEND - timedelta(microseconds=1)
    early = book.debit("trace:2", 1_000, at=almost, effect_kind="trace")
    assert (early.debited_mc, early.debt_mc) == (0, 1_000)
    assert book.lot(renewal).balance_mc == 5_000_000


def test_from_cycle_start_the_catch_up_settles_the_debt_before_the_debit() -> None:
    """MEDIUM-9: the early renewal does not pay open debt before cycle_start;
    from cycle_start on, the first operation (here a debit) first catches
    the started allowance up -- the debt settled as of the start, under the
    allowance's start-settlement effect (owner decision 2026-10-28, option
    2) -- and only then debits."""

    book = _book()
    book.debit("trace:old", 300_000, at=T0, effect_kind="trace")  # no capacity
    renewal = _monthly(book, start=NEXT_START, end=NEXT_END, at=T0)
    assert book.open_debt_mc == 300_000  # the grant settled nothing
    assert book.writes.settlements == []
    assert book.lot(renewal).balance_mc == 5_000_000
    assert book.settle_open_debts("cycle-opening", at=T0, effect_kind="x") == 0
    assert "cycle-opening" not in book.writes.effect_ids
    before = len(book.writes.entries)
    result = book.debit("trace:new", 200_000, at=NEXT_SPEND, effect_kind="trace")
    assert (result.status, result.debited_mc, result.debt_mc) == ("applied", 200_000, 0)
    assert result.at == NEXT_SPEND
    assert [
        (e["entry_type"], e["lot_id"], e["amount_mc"])
        for e in book.writes.entries[before:]
    ] == [
        ("debt_settlement", renewal, 300_000),
        ("debit", renewal, 200_000),
    ]
    assert book.writes.entries[before]["effect_id"] == (
        cycle_start_settlement_effect_id(SUB, NEXT_START, NEXT_END)
    )
    assert book.writes.entries[before]["occurred_at"] == NEXT_SPEND
    assert book.open_debt_mc == 0
    assert book.lot(renewal).balance_mc == 4_500_000


def _won_at_once(book: LedgerBook, lot_id: UUID, at: datetime) -> None:
    """A 1 mc reversal of the line and its win, both at ``at``."""

    for revision, (reversal, restoration) in enumerate(((1, 0), (1, 1)), start=1):
        book.apply_line_targets(
            lot_id,
            line_id="plan",
            payment_id="pay",
            financial_revision=revision,
            reversal_target_mc=reversal,
            restoration_target_mc=restoration,
            at=at,
        )


def _started_debt_history(touch_at_start: bool) -> tuple[LedgerBook, UUID]:
    """1 mc of debt, then an early renewal (granted before its cycle); at the
    renewal's spend start either a line is reversed and won back at once
    (``touch_at_start``) or nothing happens; then nothing until well after
    ``cycle_end``, when the deadline sweep runs."""

    book = _book()
    book.debit("trace:old", 1, at=T0, effect_kind="trace")  # no capacity: debt
    renewal = _monthly(book, grant=10_000, start=NEXT_START, end=NEXT_END, at=T0)
    assert book.open_debt_mc == 1 and book.lot(renewal).balance_mc == 10_000
    if touch_at_start:
        _won_at_once(book, renewal, NEXT_SPEND)
    book.sweep_expired(monthly_lot_expires_at(NEXT_END) + timedelta(days=3))
    return book, renewal


def test_a_started_allowance_settles_old_debt_the_same_on_every_path() -> None:
    """Owner decision 2026-10-28 (option 2), the regression of the property
    counterexample ``[debit 1, advance, monthly (early), advance to the
    start]``: whether the first operation after the start comes at the start
    (a reversal won at once) or only after ``cycle_end`` (the sweep), the
    allowance pays the debt that existed at its start, as of its start, and
    the two histories end with the same rows and net position."""

    touched, renewal = _started_debt_history(touch_at_start=True)
    untouched, renewal_b = _started_debt_history(touch_at_start=False)
    effect = cycle_start_settlement_effect_id(SUB, NEXT_START, NEXT_END)
    for book, lot_id in ((touched, renewal), (untouched, renewal_b)):
        assert book.open_debt_mc == 0
        assert book.lot(lot_id).balance_mc == 0  # expired
        caught_up = _of_effects(book, effect)
        assert [
            (e["entry_type"], e["lot_id"], e["amount_mc"], e["occurred_at"])
            for e in caught_up
        ] == [("debt_settlement", lot_id, 1, NEXT_SPEND)]
        # Spent before its deadline and not before its start (Groups 1a-2).
        assert NEXT_SPEND < book.lot(lot_id).expires_at
        expiries = [
            e["amount_mc"]
            for e in book.writes.entries
            if e["entry_type"] == "expiry" and e["lot_id"] == lot_id
        ]
        assert expiries == [9_999]
        # A time-driven effect: never acked as the fact's or the debit's own.
        assert effect in book.writes.effect_ids
        assert effect not in book.writes.operation_effect_ids
    assert _by_effect(touched, effect) == _by_effect(untouched, effect)


def test_once_caught_up_a_start_settles_nothing_more() -> None:
    book = _book()
    book.debit("trace:old", 1, at=T0, effect_kind="trace")
    renewal = _monthly(book, grant=10_000, start=NEXT_START, end=NEXT_END, at=T0)
    book.sweep_expired(NEXT_SPEND)
    effect = cycle_start_settlement_effect_id(SUB, NEXT_START, NEXT_END)
    assert len(_of_effects(book, effect)) == 1
    before = len(book.writes.entries)
    # Debt created AFTER the start is never paid by the catch-up "as of the
    # start"; the debit pays it from what is spendable at its own instant.
    later = NEXT_SPEND + timedelta(days=1)
    result = book.debit("trace:new", 20_000, at=later, effect_kind="trace")
    assert (result.debited_mc, result.debt_mc) == (9_999, 10_001)
    book.sweep_expired(later + timedelta(hours=1))
    assert len(_of_effects(book, effect)) == 1
    assert [e["entry_type"] for e in book.writes.entries[before:]] == ["debit", "debt"]
    assert book.lot(renewal).balance_mc == 0 and book.open_debt_mc == 10_001


def test_a_refused_or_replayed_debit_catches_nothing_up() -> None:
    book = _book()
    book.debit("trace:old", 1, at=T0, effect_kind="trace")
    _monthly(book, grant=10_000, start=NEXT_START, end=NEXT_END, at=T0)
    before = len(book.writes.entries)
    replay = book.debit("trace:old", 1, at=NEXT_SPEND, effect_kind="trace")
    assert replay.status == "duplicate"
    assert len(book.writes.entries) == before and book.open_debt_mc == 1
    # An active quarantine hold refuses the debit: no row at all, not even
    # the catch-up of the allowance that started.
    held = _book()
    held.debit("trace:old", 1, at=T0, effect_kind="trace")
    _monthly(held, grant=10_000, start=NEXT_START, end=NEXT_END, at=T0)
    held.spend_hold = SpendHold(quarantined=True, enforced=True)
    rows = {name: len(rows) for name, rows in vars(held.writes).items()}
    refused = held.debit("trace:new", 1, at=NEXT_SPEND, effect_kind="trace")
    assert refused.status == "credit_pending"
    assert {name: len(rows) for name, rows in vars(held.writes).items()} == rows
    assert held.open_debt_mc == 1
    effect = cycle_start_settlement_effect_id(SUB, NEXT_START, NEXT_END)
    assert effect not in held.writes.effect_ids


def test_a_grant_after_the_start_catches_the_allowance_up_first() -> None:
    """The first operation after the start is a top-up grant: the started
    allowance pays the old debt as of its start, under its own effect; the
    grant's own settlement finds nothing left."""

    book = _book()
    book.debit("trace:old", 1_000, at=T0, effect_kind="trace")
    renewal = _monthly(book, grant=10_000, start=NEXT_START, end=NEXT_END, at=T0)
    later = NEXT_SPEND + timedelta(days=1)
    order = uuid4()
    top = _topup(book, grant=500, paid_at=civil_date(later), at=later, order_id=order)
    effect = cycle_start_settlement_effect_id(SUB, NEXT_START, NEXT_END)
    assert [
        (e["entry_type"], e["lot_id"], e["amount_mc"], e["occurred_at"])
        for e in _of_effects(book, effect)
    ] == [("debt_settlement", renewal, 1_000, NEXT_SPEND)]
    assert [
        e["entry_type"] for e in _of_effects(book, topup_grant_effect_id(order))
    ] == ["grant"]
    assert book.lot(top).balance_mc == 500 and book.open_debt_mc == 0
    assert effect not in book.writes.operation_effect_ids


@pytest.mark.parametrize("oldest_at_start", [False, True])
def test_a_loaded_book_pays_only_the_debt_that_existed_at_the_start(
    oldest_at_start: bool,
) -> None:
    """A book loaded from rows written before the catch-up existed (Group 2
    left open debt next to a started allowance): the first operation pays,
    dated at the start, exactly the debt created at or before the start --
    one created exactly at the start included, one created after it never
    (``debts_until``) -- even when the sweep runs after the allowance
    expired."""

    renewal = LotState(
        id=uuid4(),
        lot_kind="monthly",
        paid_at=NEXT_START,
        expires_at=monthly_lot_expires_at(NEXT_END),
        granted_mc=10_000,
        grant_effect_id=monthly_grant_effect_id(SUB, NEXT_START, NEXT_END),
        subscription_id=SUB,
        cycle_start=NEXT_START,
        cycle_end=NEXT_END,
        sums=Counter({"grant": 10_000}),
    )

    def debt(at: datetime, amount: int, name: str) -> DebtState:
        return DebtState(
            id=uuid4(),
            created_at=at,
            source_kind="consumption",
            source_effect_id=name,
            original_lot_id=None,
            amount_mc=amount,
        )

    debts = [debt(NEXT_SPEND, 300, "trace:at-start")]
    if not oldest_at_start:
        debts.append(debt(T0, 500, "trace:before"))
    after = debt(NEXT_SPEND + timedelta(days=1), 200, "trace:after")
    debts.append(after)
    book = LedgerBook(
        builder_id=uuid4(),
        account_id=uuid4(),
        lots=[renewal],
        debts=debts,
        known_effects=[renewal.grant_effect_id] + [d.source_effect_id for d in debts],
    )
    book.sweep_expired(monthly_lot_expires_at(NEXT_END) + timedelta(days=1))
    effect = cycle_start_settlement_effect_id(SUB, NEXT_START, NEXT_END)
    paid = 300 if oldest_at_start else 800
    assert [
        (e["entry_type"], e["amount_mc"], e["occurred_at"])
        for e in _of_effects(book, effect)
    ] == [
        ("debt_settlement", amount, NEXT_SPEND)
        for amount in ([300] if oldest_at_start else [500, 300])
    ]
    assert {row["debt_id"] for row in book.writes.settlements} == {
        d.id for d in debts if d is not after
    }
    assert book.open_debt_by_id()[after.id] == 200
    assert book.open_debt_mc == 200
    expiries = [
        e["amount_mc"] for e in book.writes.entries if e["entry_type"] == "expiry"
    ]
    assert expiries == [10_000 - paid]


def _count_passes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count the passes over the debts (``_open_by_debt``) and the catch-up
    settlements (``_settle`` with ``debts_until``) a book makes."""

    passes = {"open": 0, "catch_up": 0}
    open_by_debt = LedgerBook._open_by_debt
    settle = LedgerBook._settle

    def counted_open(self: LedgerBook) -> dict[UUID, int]:
        passes["open"] += 1
        return open_by_debt(self)

    def counted_settle(self: LedgerBook, *args: Any, **kwargs: Any) -> int:
        if kwargs.get("debts_until") is not None:
            passes["catch_up"] += 1
        return settle(self, *args, **kwargs)

    monkeypatch.setattr(LedgerBook, "_open_by_debt", counted_open)
    monkeypatch.setattr(LedgerBook, "_settle", counted_settle)
    return passes


def test_the_catch_up_costs_one_debt_pass_whatever_the_allowance_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-1: the catch-up runs on every operation under the account lock.
    On a 24-month book with hundreds of debts, all settled, it reads the
    open debts at most once and settles nothing -- not one pass per
    allowance; with open debt newer than every start it still visits no
    start.  Upper bounds: an equivalent shortcut may read less."""

    book = _book(clock_floor=datetime(2024, 1, 1, tzinfo=timezone.utc))
    first = date(2024, 1, 12)
    for month in range(24):
        start = add_calendar_months(first, month)
        end = add_calendar_months(first, month + 1)
        _monthly(
            book,
            grant=5_000 if month == 0 else 10_000,
            start=start,
            end=end,
            at=start_of_civil_day(start),
        )
        during = start_of_civil_day(start) + timedelta(hours=1)
        for serial in range(40):
            book.debit(f"trace:{month}:{serial}", 250, at=during, effect_kind="t")
    last = start_of_civil_day(add_calendar_months(first, 24))
    _topup(book, grant=10_000, paid_at=civil_date(last), at=last)
    assert len(book.debts) == 24 * 20 and book.open_debt_mc == 0
    assert sum(lot.lot_kind == "monthly" for lot in book.lots) == 24

    passes = _count_passes(monkeypatch)
    book.sweep_expired(last + timedelta(hours=1))
    assert passes["open"] <= 1 and passes["catch_up"] == 0
    before = len(book.writes.entries)
    book.debit("trace:big", 20_000, at=last + timedelta(hours=2), effect_kind="t")
    # At most the catch-up's one read, then the debit's own settlement.
    assert passes["open"] <= 3 and passes["catch_up"] == 0
    assert [e["entry_type"] for e in book.writes.entries[before:]] == ["debit", "debt"]
    # Open debt now, newer than every start: still no start is visited.
    book.sweep_expired(last + timedelta(hours=3))
    assert passes["open"] <= 4 and passes["catch_up"] == 0
    assert book.open_debt_mc == 15_000


def test_the_catch_up_visits_no_start_where_nothing_can_pay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM-1/2: an old open debt that predates 24 starts, every lot spent
    on it.  No operation settles at any of those starts -- neither with no
    lot holding a balance, nor once a renewal granted before its own start
    holds one (spendable at none of the past starts) -- and the passes over
    the debts stay bounded, whatever the length of the history."""

    book = _book(clock_floor=datetime(2024, 1, 1, tzinfo=timezone.utc))
    first = date(2024, 1, 12)
    old_at = start_of_civil_day(first) - timedelta(days=1)
    book.debit("trace:old", 1_000_000, at=old_at, effect_kind="t")
    for month in range(24):
        start = add_calendar_months(first, month)
        end = add_calendar_months(first, month + 1)
        _monthly(book, grant=10_000, start=start, end=end, at=start_of_civil_day(start))
    assert book.open_debt_mc == 1_000_000 - 24 * 10_000
    assert all(lot.balance_mc == 0 for lot in book.lots)
    now = start_of_civil_day(add_calendar_months(first, 23)) + timedelta(hours=1)

    passes = _count_passes(monkeypatch)
    book.sweep_expired(now)  # every lot spent
    assert passes["catch_up"] == 0 and passes["open"] <= 1
    renewal = _monthly(
        book,
        grant=10_000,
        start=add_calendar_months(first, 24),
        end=add_calendar_months(first, 25),
        at=now + timedelta(hours=1),
    )
    book.sweep_expired(now + timedelta(hours=2))
    result = book.debit("trace:new", 1, at=now + timedelta(hours=3), effect_kind="t")
    assert (result.debited_mc, result.debt_mc) == (0, 1)
    assert passes["catch_up"] == 0
    # sweep, grant (its own settlement), sweep, debit (catch-up + its own).
    assert passes["open"] <= 5
    assert book.lot(renewal).balance_mc == 10_000
    assert book.open_debt_mc == 1_000_000 - 24 * 10_000 + 1


def test_the_catch_up_counts_what_is_open_in_millicredits() -> None:
    """LOW-1: two debts of 5 mc; then a 2 mc and a 100 mc allowance start
    between operations.  The first start pays 2 mc and leaves 8 mc open, so
    the pass must go on to the second start (counting open DEBTS instead of
    millicredits would stop after the first and leave 8 mc open next to
    100 mc spendable)."""

    book = _book()
    book.debit("trace:a", 5, at=T0, effect_kind="trace")
    book.debit("trace:b", 5, at=T0, effect_kind="trace")
    later_end = add_calendar_months(NEXT_END, 1)
    small = _monthly(book, grant=2, start=NEXT_START, end=NEXT_END, at=T0)
    large = _monthly(book, grant=100, start=NEXT_END, end=later_end, at=T0)
    assert book.open_debt_mc == 10
    second = start_of_civil_day(NEXT_END)
    book.sweep_expired(second + timedelta(hours=1))
    first_effect = cycle_start_settlement_effect_id(SUB, NEXT_START, NEXT_END)
    second_effect = cycle_start_settlement_effect_id(SUB, NEXT_END, later_end)
    assert [
        (e["lot_id"], e["amount_mc"], e["occurred_at"])
        for e in _of_effects(book, first_effect)
    ] == [(small, 2, NEXT_SPEND)]
    assert [
        (e["lot_id"], e["amount_mc"], e["occurred_at"])
        for e in _of_effects(book, second_effect)
    ] == [(large, 3, second), (large, 5, second)]
    assert book.open_debt_mc == 0 and book.lot(large).balance_mc == 92
    assert {first_effect, second_effect} <= set(book.writes.swept_effect_ids)


def test_a_lagging_writer_acts_at_the_ledger_clock_for_o5_too() -> None:
    """``DebitResult.at``, not the caller's instant, decides spendability."""

    book = _book(clock_floor=NEXT_SPEND + timedelta(hours=1))
    renewal = _monthly(book, start=NEXT_START, end=NEXT_END, at=T0)
    result = book.debit("trace:1", 1_000, at=T0, effect_kind="trace")
    assert result.at == NEXT_SPEND + timedelta(hours=1)
    assert (result.debited_mc, result.debt_mc) == (1_000, 0)
    assert book.lot(renewal).balance_mc == 4_999_000


def test_a_refund_before_cycle_start_withdraws_the_lot_and_owes_nothing() -> None:
    """HIGH-2: ``LotState.available_mc`` stays blind to cycle_start, so a
    reversal before the cycle starts takes the allowance back, no debt."""

    book = _book()
    renewal = _monthly(book, start=NEXT_START, end=NEXT_END, at=T0)
    application = _targets(book, renewal, 5_000_000)
    assert getattr(application, "reversal_delta_mc") == 5_000_000
    assert book.lot(renewal).balance_mc == 0
    assert book.open_debt_mc == 0 and book.debts == ()
    assert _by_effect(book, "reversal:") == Counter({"reversal": 5_000_000})
    # Partial refund before the start: the rest stays for the cycle.
    book2 = _book()
    renewal2 = _monthly(book2, start=NEXT_START, end=NEXT_END, at=T0)
    _targets(book2, renewal2, 2_000_000)
    assert book2.lot(renewal2).balance_mc == 3_000_000 and book2.open_debt_mc == 0
    assert book2.available_mc(NEXT_SPEND) == 3_000_000


def test_the_previous_allowance_expires_when_the_renewal_starts() -> None:
    book = _book()
    current = _monthly(book, grant=1_000_000)
    renewal = _monthly(book, start=NEXT_START, end=NEXT_END, at=T0)
    last = NEXT_SPEND - timedelta(microseconds=1)
    assert book.available_mc(last) == 1_000_000  # only the current cycle
    assert book.available_mc(NEXT_SPEND) == 5_000_000  # only the renewal
    book.sweep_expired(NEXT_SPEND)
    assert book.lot(current).balance_mc == 0
    result = book.debit("trace:1", 1_000, at=NEXT_SPEND, effect_kind="trace")
    assert (result.debited_mc, result.debt_mc) == (1_000, 0)
    assert book.lot(renewal).balance_mc == 4_999_000


def test_the_spend_start_follows_sao_paulo_dst_days() -> None:
    """A cycle starting on a DST-start day (midnight did not exist in
    2018-11-04): the civil day began at 01:00 -02, i.e. 03:00 UTC."""

    book = _book(clock_floor=datetime(2018, 10, 1, tzinfo=timezone.utc))
    lot_id = _monthly(
        book,
        start=date(2018, 11, 4),
        end=date(2018, 12, 4),
        at=datetime(2018, 10, 1, tzinfo=timezone.utc),
    )
    start = datetime(2018, 11, 4, 3, 0, tzinfo=timezone.utc)
    assert book.lot(lot_id).spend_starts_at() == start
    assert book.lot(lot_id).spendable_mc(start - timedelta(seconds=1)) == 0
    assert book.lot(lot_id).spendable_mc(start) == 5_000_000


async def test_the_adapter_debit_applies_o5_at_the_instant_it_acts_at(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    builder_id = uuid4()
    async with sessions() as session, session.begin():
        ledger = _ledger(session, T0)
        book = await ledger.open_book(builder_id, at=T0)
        renewal = _monthly(book, start=NEXT_START, end=NEXT_END, at=T0)
        await ledger.persist(book)
    async with sessions() as session, session.begin():
        early = await _ledger(session, T0).debit(
            builder_id, "trace:early", 1_000, at=T0, effect_kind="trace"
        )
    assert (early.status, early.debited_mc, early.debt_mc) == ("applied", 0, 1_000)
    async with sessions() as session, session.begin():
        late = await _ledger(session, NEXT_SPEND).debit(
            builder_id, "trace:late", 2_000, at=NEXT_SPEND, effect_kind="trace"
        )
    assert (late.debited_mc, late.debt_mc, late.at) == (2_000, 0, NEXT_SPEND)
    async with sessions() as session, session.begin():
        reloaded = await _state(session, builder_id, NEXT_SPEND)
        assert reloaded.open_debt_mc == 0
        assert reloaded.lot(renewal).balance_mc == 5_000_000 - 3_000
        moves = Counter(
            (
                await session.execute(
                    select(CreditLedgerEntry.entry_type).where(
                        CreditLedgerEntry.builder_id == builder_id
                    )
                )
            ).scalars()
        )
    assert moves == Counter({"grant": 1, "debt": 1, "debt_settlement": 1, "debit": 1})
