"""Credit ledger rules (neurostack-credit-system Group 2).

Grants, expiry, monthly-then-FEFO consumption, cumulative reversal, debt,
debt settlement and restoration, all as append-only rows of migration 038.
Amounts are integer millicredits (1 credit = 1,000 mc); no float anywhere.

Two layers:

* :class:`LedgerBook` -- the rules, with no I/O.  It holds one builder's
  state as loaded under the account lock, applies operations in memory and
  records the rows they need (:class:`LedgerWrites`).  The property tests
  drive it directly.
* :class:`CreditLedger` -- the database adapter.  ``open_book`` follows the
  lock order of :mod:`src.repositories.credit` (account ``FOR UPDATE``, the
  read-only quarantine hold, then the lots ``FOR UPDATE``), loads the state
  and sweeps due expiries; ``persist`` inserts the recorded rows in foreign
  key order.  It never commits: the caller owns the transaction.

Time.  The civil calendar is ``America/Sao_Paulo``; instants are persisted in
UTC.  A monthly lot expires at the first instant of its ``cycle_end`` day; a
top-up lot at the first instant of ``paid_at`` + 6 calendar months (the day
clamped to the last day of the target month).  Expiry is explicit: the first
writer that holds the account lock after the deadline appends one ``expiry``
entry of the remaining balance (``occurred_at`` = the deadline, effect
``expiry:{lot_id}``).  An expired lot is never usable, whether or not that
entry exists yet.

One builder's ledger time never goes back.  Writers of a builder serialize
on its account lock, but their clocks may disagree (ECS tasks skew; a worker
may read its clock and then wait on the lock): a writer whose clock is behind
the latest ``occurred_at`` already in the builder's ledger acts at that
instant instead (:attr:`LedgerBook.clock`).  Otherwise it could see a lot as
unexpired after another writer had already recorded the lot's expiry, and a
restoration would put usable balance back into an expired lot.  As a second
guard a restoration also treats as expired any lot whose ``expiry`` effect
already exists.  Against the DATABASE clock (tolerance: setting
``credit_clock_tolerance_seconds``, 5 minutes by default, at most 10),
:class:`CreditLedger` never refuses a writer for its own instant: one more
than the tolerance ahead acts at ``database clock + tolerance``, with a
warning (owner decision C), and one more than the tolerance behind at
``database clock - tolerance`` (owner decision A).  The one refusal left
(:class:`LedgerClockAhead`, writing nothing) is a builder whose ledger clock
is already beyond ``database clock + tolerance`` (the database clock stepped
back): its writers are refused until the database clock catches up, and
nothing is repaired (no such row exists on ``main``).

Determinism.  Every tie is broken by a stable value -- lots by their grant
effect id (which names the cycle or the order), debts by their line's grant
effect id, the cumulative target and the effect that created them -- never
by a random row id, so one history always produces the same rows.

Consumption order.  The monthly allowance first, then top-ups by the
earliest ``expires_at``, ties by ``paid_at`` and ``id``; expired lots and
other builders' lots are never read.  A shortfall becomes ``consumption``
debt.  Open debt is settled from usable capacity whenever capacity appears
(grant, restoration) or debt appears (reversal, debit), oldest debt first
(``created_at``, then the tie-break above), each settlement recorded in
``credit_debt_settlements`` with the lot that paid it; so after every
operation open debt and usable capacity never coexist (a monthly allowance
that started since the previous operation is caught up first, O5 below).
A refused or replayed debit writes
nothing at all (:meth:`CreditLedger.debit` does not persist even the deadline
sweep its book made).

O5, a SPEND rule only (owner decision, 2026-10-26; Group 3a).  A monthly
allowance may be granted before its cycle starts (an early renewal), but
debits and debt settlement use it only from ``start_of_civil_day(
cycle_start)`` on, compared with the instant the operation acts at
(``DebitResult.at``): :meth:`LotState.spendable_mc` feeds :meth:`debit`,
:meth:`settle_open_debts` and :meth:`LedgerBook.available_mc` (the figure of
the APIs and the UI).  :meth:`LotState.available_mc` is unchanged: a reversal
withdraws a not-yet-started allowance like any other (a refund before
``cycle_start`` takes the lot back and creates no debt).  So an early renewal
with open debt does not settle it before ``cycle_start``.

The start catch-up (owner decision, 2026-10-28, option 2).  An allowance
starting is the one way open debt and usable capacity come to coexist with
no operation in between.  So EVERY operation -- the deadline sweep (before
it expires anything), a grant, a settlement, a line's reversal/restoration,
an accepted debit -- first settles, for each monthly allowance whose
``start_of_civil_day(cycle_start) <= at`` (the operation's monotonic
instant), the debt that already existed at that start, from the capacity
spendable AT that start, as of that start: its entries are dated at the
start (as an ``expiry`` is dated at its deadline), under the deterministic
``cycle:...:start-settlement`` effect of that allowance (a time-driven
effect, like the sweep's expiries: it is not the effect of the fact or the
debit at hand).  Whether that operation comes on the start day or after
``cycle_end``, the rows and the net position are the same, so equivalent
histories end equal; once caught up, a start settles nothing more (no-op).

Reversal (cumulative per credit-bearing line, ``reversal_target_mc =
floor(grant_mc x reversed_cents / settled_cents)``).  A target increase D
withdraws first what is still available (``reversal`` entry), then absorbs
capacity of the line that had already expired unused (nothing to withdraw,
no entry), and turns the rest -- consumption the refunded payment funded --
into ``reversal`` debt linked to the original lot and to the consumption
entries it came from.  (Owner decision O1, 2026-10-26: reversing a lot that
already expired takes the available part, then the part that expired unused;
only what was consumed becomes debt.)

Restoration (``restoration_target_mc``, never above the reversal target).
A target increase R first releases this line's reversal debt in force, then
regrants the original lot (``lot_regrant``).  The debt is released newest
debt first -- a restoration undoes the latest reversal first, so a reversal
won at once leaves every lot as it was -- each debt's open part
(``debt_release``) before its settled part, which goes back to exactly the
lots that paid THAT debt (``settlement_release``, latest-expiring lot
first).  Each released debt's entries hang from their own effect
``return:{debt_id}:{sha256(trigger effect id)}``, so every release is tied
to its debt (also on reload; no chronology is inferred).  A destination lot
already expired receives the amount and an equal ``expiry`` in the same
effect, so nothing usable reappears.  ``debt_release + settlement_release +
lot_regrant = R`` exactly, once (over the restoration and its ``return:``
effects).  The return cascades (owner decisions, option (a) and E): when the
paying lot was itself charged back, the returned amount first cancels the
payer's reversal debt in force -- the payer withdraws it again
(``reversal``, under ``release:{payer_lot}:{sha256(trigger effect id)}``)
and releases as much of its own debt the same way -- so capacity always
lands on the lot that really paid: no line holds usable capacity while its
own reversal debt is in force, and none owes more reversal debt than it
consumed.

Idempotency.  Every change hangs from a ``credit_effects`` row,
``UNIQUE(builder_id, effect_id)``.  Reversal and restoration effect ids carry
the cumulative target they reached (``reversal:{lot_id}:{line_id}:
{payment_id}:{financial_revision}:{target_mc}``), so the applied target of a
line is the largest one among its effects: a replay or an older revision
applies nothing and a target never regresses.

Quarantine hold.  Read-only, under the account lock, before the lot locks:
a builder with a ``quarantined`` ASAAS order or cycle.  It acts only after
``credit_enforcement_at``: then debits (and, from Group 4a, reservations)
answer ``credit_pending`` without touching the ledger; grants, reversals and
restorations keep applying.  Before the enforcement it is computed only.

Notes for later groups.  :class:`DebitResult` carries ``at``, the instant the
debit acted at (after the clock rule); the O5 predicate above is evaluated
at that instant, inside the book.  A refused debit writes nothing, so for a
builder on hold the stored ledger (``derived_balance``) can lag behind
expiries the sweep has not persisted: APIs and the UI (Groups 6, 7) show
usable capacity from ``LedgerBook.available_mc(at)``, never the derived
balance as usable.  Likewise the STORED open debt (``credit_debts`` and the
derived debt) lags an allowance that started since the last write until the
next writer persists its start catch-up: Group 5 (suspension on debt) and
Group 7 must read open debt through a swept book (``LedgerBook.open_debt_mc``
after :meth:`LedgerBook.sweep_expired` at the instant they judge), as they
already must for expiries, never from the stored rows alone.

Group 2 never charges RAG or media maintenance, never opens an exhaustion
episode and never reads ``tests/``.
"""

from __future__ import annotations

import calendar
import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Final, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.config.settings import settings
from src.models.credit import (
    CreditDebt,
    CreditDebtOrigin,
    CreditDebtSettlement,
    CreditEffect,
    CreditLedgerEntry,
    CreditLot,
)
from src.repositories.credit import CreditRepository

logger = get_logger(__name__)

CREDIT_TIMEZONE: Final = ZoneInfo("America/Sao_Paulo")
MILLICREDITS_PER_CREDIT: Final = 1_000
TOPUP_VALIDITY_MONTHS: Final = 6

# Effect kinds this module writes (``credit_effects.effect_kind``).  Debits
# carry the caller's kind (``trace``, ``rag`` ... from Group 3 on).
GRANT_EFFECT: Final = "grant"
REVERSAL_EFFECT: Final = "reversal"
RESTORATION_EFFECT: Final = "restoration"
EXPIRY_EFFECT: Final = "expiry"
DEBT_SETTLEMENT_EFFECT: Final = "debt_settlement"
# A cascaded release of a line's reversal debt (see ``_credit_payer``).
RELEASE_EFFECT: Final = "release"
# The release of one debt by a restoration or release (``_release_line_debt``).
RETURN_EFFECT: Final = "return"
LEDGER_EFFECT_KINDS: Final = (
    GRANT_EFFECT,
    REVERSAL_EFFECT,
    RESTORATION_EFFECT,
    EXPIRY_EFFECT,
    DEBT_SETTLEMENT_EFFECT,
    RELEASE_EFFECT,
    RETURN_EFFECT,
)

# How far a writer's clock may stray from the database clock (owner
# decisions, 2026-10-26: 5 minutes, at most 10, configurable; further ahead
# is limited to the bound with a warning, further behind is raised to it):
# the setting ``credit_clock_tolerance_seconds`` (1..600, default 300), which
# ``CreditLedger`` reads unless a caller passes an override.
MIN_CLOCK_TOLERANCE: Final = timedelta(seconds=1)
MAX_CLOCK_TOLERANCE: Final = timedelta(minutes=10)


def configured_clock_tolerance() -> timedelta:
    """``settings.credit_clock_tolerance_seconds`` as a duration."""

    return timedelta(seconds=settings.credit_clock_tolerance_seconds)


# ``credit_debts.debt_source_kind``.
CONSUMPTION_DEBT: Final = "consumption"
REVERSAL_DEBT: Final = "reversal"

LotKind = Literal["monthly", "topup"]
DebitStatus = Literal["applied", "duplicate", "credit_pending"]


class LedgerInvariantError(RuntimeError):
    """The stored ledger contradicts a rule of this module (never expected).

    Raised instead of writing a row that would hide the contradiction; the
    caller's transaction rolls back.
    """


class LedgerClockAhead(RuntimeError):
    """The builder's ledger clock (its latest ``occurred_at``) is already
    beyond ``database clock + tolerance``: no writer may build on it until
    the database clock catches up, and nothing is recorded (fail closed; the
    caller's transaction rolls back).  A writer's own instant is never
    refused (:meth:`CreditLedger.checked_instant` limits it)."""


# ---------------------------------------------------------------------------
# Calendar and money rules (pure).
# ---------------------------------------------------------------------------


def add_calendar_months(day: date, months: int) -> date:
    """``day`` plus ``months`` calendar months, the day clamped to the month."""

    if months < 0:
        raise ValueError("months must not be negative")
    index = day.year * 12 + (day.month - 1) + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def start_of_civil_day(day: date, zone: ZoneInfo = CREDIT_TIMEZONE) -> datetime:
    """First instant, in UTC, of the civil day ``day`` in ``zone``.

    ``fold=0`` resolves an ambiguous midnight to its first occurrence and a
    nonexistent one (``America/Sao_Paulo`` started DST at 00:00 until 2019)
    with the offset in force before the jump, which is the instant the day
    began.  The result is checked to really be on ``day``.
    """

    instant = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
    if instant.astimezone(zone).date() != day:
        raise ValueError(f"cannot place the start of {day.isoformat()} in {zone}")
    return instant


def civil_date(instant: datetime, zone: ZoneInfo = CREDIT_TIMEZONE) -> date:
    """The civil date of an aware ``instant`` in ``zone``."""

    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(zone).date()


def monthly_lot_expires_at(cycle_end: date) -> datetime:
    """A monthly allowance expires when its ``cycle_end`` day begins.

    The lot is granted when its cycle settles, which may be before
    ``cycle_start`` (an early renewal); Group 2 does not change that.  Using
    the allowance only from ``cycle_start`` on is a restriction of spending
    (:meth:`LotState.spendable_mc`, Group 3a), not of the grant (owner
    decision O5, 2026-10-26).
    """

    return start_of_civil_day(cycle_end)


def topup_lot_expires_on(paid_at: date) -> date:
    """``paid_at`` + 6 calendar months, clamped to the target month."""

    return add_calendar_months(paid_at, TOPUP_VALIDITY_MONTHS)


def topup_lot_expires_at(paid_at: date) -> datetime:
    """A top-up lot expires when its ``expires_on`` day begins."""

    return start_of_civil_day(topup_lot_expires_on(paid_at))


def cumulative_target_mc(granted_mc: int, cents: int, settled_cents: int) -> int:
    """``floor(granted_mc x cents / settled_cents)`` in integers."""

    if type(granted_mc) is not int or granted_mc <= 0:
        raise ValueError("granted_mc must be a positive integer")
    if type(settled_cents) is not int or settled_cents <= 0:
        raise ValueError("settled_cents must be a positive integer")
    if type(cents) is not int or not 0 <= cents <= settled_cents:
        raise ValueError("cents must be an integer within [0, settled_cents]")
    return granted_mc * cents // settled_cents


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Effect ids (stable: a replay of the same operation produces the same id).
# ---------------------------------------------------------------------------


def monthly_grant_effect_id(
    subscription_id: UUID, cycle_start: date, cycle_end: date
) -> str:
    return (
        f"cycle:{subscription_id}:{cycle_start.isoformat()}:"
        f"{cycle_end.isoformat()}:grant"
    )


def cycle_debt_settlement_effect_id(
    subscription_id: UUID, cycle_start: date, cycle_end: date
) -> str:
    return (
        f"cycle:{subscription_id}:{cycle_start.isoformat()}:"
        f"{cycle_end.isoformat()}:debt-settlement"
    )


def cycle_start_settlement_effect_id(
    subscription_id: UUID, cycle_start: date, cycle_end: date
) -> str:
    """The start catch-up of a monthly allowance (see the module docstring)."""

    return (
        f"cycle:{subscription_id}:{cycle_start.isoformat()}:"
        f"{cycle_end.isoformat()}:start-settlement"
    )


def topup_grant_effect_id(order_id: UUID) -> str:
    return f"topup:{order_id}:grant"


def expiry_effect_id(lot_id: UUID) -> str:
    return f"expiry:{lot_id}"


def _line_effect_id(
    kind: str,
    lot_id: UUID,
    *,
    line_id: str,
    payment_id: str,
    financial_revision: int,
    target_mc: int,
) -> str:
    return f"{kind}:{lot_id}:{line_id}:{payment_id}:{financial_revision}:{target_mc}"


def reversal_effect_id(
    lot_id: UUID,
    *,
    line_id: str,
    payment_id: str,
    financial_revision: int,
    target_mc: int,
) -> str:
    return _line_effect_id(
        REVERSAL_EFFECT,
        lot_id,
        line_id=line_id,
        payment_id=payment_id,
        financial_revision=financial_revision,
        target_mc=target_mc,
    )


def restoration_effect_id(
    lot_id: UUID,
    *,
    line_id: str,
    payment_id: str,
    financial_revision: int,
    target_mc: int,
) -> str:
    return _line_effect_id(
        RESTORATION_EFFECT,
        lot_id,
        line_id=line_id,
        payment_id=payment_id,
        financial_revision=financial_revision,
        target_mc=target_mc,
    )


def release_effect_id(lot_id: UUID, trigger_effect_id: str) -> str:
    """The cascaded release of ``lot_id``'s reversal debt that the effect
    ``trigger_effect_id`` set off (a digest keeps nested ids short)."""

    digest = hashlib.sha256(trigger_effect_id.encode("utf-8")).hexdigest()
    return f"{RELEASE_EFFECT}:{lot_id}:{digest}"


def return_effect_id(debt_id: UUID, trigger_effect_id: str) -> str:
    """The release of debt ``debt_id`` that the restoration or release
    ``trigger_effect_id`` set off: its ``debt_release`` and
    ``settlement_release`` entries hang from it, so each release is tied to
    its debt."""

    digest = hashlib.sha256(trigger_effect_id.encode("utf-8")).hexdigest()
    return f"{RETURN_EFFECT}:{debt_id}:{digest}"


def returned_debt(effect_id: str) -> UUID | None:
    """The debt a ``return:`` effect releases (``None`` for other effects)."""

    kind, _, rest = effect_id.partition(":")
    if kind != RETURN_EFFECT:
        return None
    debt_text = rest.partition(":")[0]
    try:
        debt_id = UUID(debt_text)
    except ValueError:
        return None
    return debt_id if str(debt_id) == debt_text else None


def parse_line_effect_id(effect_id: str) -> tuple[str, UUID, int] | None:
    """``(kind, lot_id, target_mc)`` of a reversal/restoration effect id."""

    kind, _, rest = effect_id.partition(":")
    if kind not in (REVERSAL_EFFECT, RESTORATION_EFFECT):
        return None
    lot_text, _, tail = rest.partition(":")
    target_text = tail.rpartition(":")[2]
    try:
        lot_id = UUID(lot_text)
    except ValueError:
        return None
    if not target_text.isdigit() or str(lot_id) != lot_text:
        return None
    return kind, lot_id, int(target_text)


# ---------------------------------------------------------------------------
# In-memory state and the rows an operation records.
# ---------------------------------------------------------------------------

_LOT_PLUS: Final = ("grant", "settlement_release", "lot_regrant")
_LOT_MINUS: Final = ("debit", "expiry", "reversal", "debt_settlement")


@dataclass
class LotState:
    """One lot and the per-type sums of the ledger entries that name it."""

    id: UUID
    lot_kind: str
    paid_at: date
    expires_at: datetime
    granted_mc: int
    grant_effect_id: str
    payment_id: str = ""
    subscription_id: UUID | None = None
    cycle_start: date | None = None
    cycle_end: date | None = None
    order_id: UUID | None = None
    sums: Counter[str] = field(default_factory=Counter)

    @property
    def balance_mc(self) -> int:
        return sum(self.sums[t] for t in _LOT_PLUS) - sum(
            self.sums[t] for t in _LOT_MINUS
        )

    @property
    def consumed_mc(self) -> int:
        """Capacity this lot spent: debits and debts it paid, net of the
        settlements a restoration returned to it."""

        return (
            self.sums["debit"]
            + self.sums["debt_settlement"]
            - self.sums["settlement_release"]
        )

    def expired(self, at: datetime) -> bool:
        return self.expires_at <= at

    def available_mc(self, at: datetime) -> int:
        """What the lot holds at ``at``: what a reversal withdraws.

        Deliberately blind to ``cycle_start`` (O5 is a SPEND rule only): a
        refund before the cycle starts must withdraw the lot, not turn into
        debt."""

        return 0 if self.expired(at) else max(self.balance_mc, 0)

    def spend_starts_at(self) -> datetime | None:
        """First instant debits and debt settlement may use this lot: the
        civil start of ``cycle_start`` for a monthly allowance (owner decision
        O5), ``None`` (no start) otherwise."""

        if self.lot_kind != "monthly" or self.cycle_start is None:
            return None
        return start_of_civil_day(self.cycle_start)

    def spendable_mc(self, at: datetime) -> int:
        """What debits and debt settlement may use at ``at`` (O5).

        Zero for a monthly allowance before its ``cycle_start`` (an early
        renewal is granted before its cycle begins), otherwise exactly
        :meth:`available_mc`."""

        starts = self.spend_starts_at()
        if starts is not None and at < starts:
            return 0
        return self.available_mc(at)

    def fefo_key(self) -> tuple[int, datetime, date, str]:
        """Monthly first, then the earliest expiry, then ``paid_at``, then the
        grant effect id -- stable (it names the cycle or order), never the
        random lot id, so the same history always consumes the same lots."""

        return (
            0 if self.lot_kind == "monthly" else 1,
            self.expires_at,
            self.paid_at,
            self.grant_effect_id,
        )


@dataclass
class DebtState:
    """One debt and how much each lot settled of it."""

    id: UUID
    created_at: datetime
    source_kind: str
    source_effect_id: str
    original_lot_id: UUID | None
    amount_mc: int
    settled_by_lot: Counter[UUID] = field(default_factory=Counter)

    @property
    def settled_mc(self) -> int:
        return sum(self.settled_by_lot.values())

    @property
    def target_mc(self) -> int:
        """The cumulative reversal target its effect reached (0 for a
        consumption debt): a line's debts are created in increasing target
        order, so it orders them exactly, newest last."""

        parsed = parse_line_effect_id(self.source_effect_id)
        return 0 if parsed is None else parsed[2]

    def order_key(self, line_key: str = "") -> tuple[datetime, str, int, str]:
        """Oldest first; ties (one instant, common under the monotonic clock)
        by ``line_key`` -- the grant effect id of the debt's line, stable,
        unlike the random lot id its reversal effect id embeds -- then by the
        cumulative target (numeric: the text of the effect id would put
        ``1000000`` before ``500000``), then by the effect that created it.
        One effect creates at most one debt."""

        return (self.created_at, line_key, self.target_mc, self.source_effect_id)


@dataclass(frozen=True)
class SpendHold:
    """Quarantine hold of one builder, read under its account lock."""

    quarantined: bool = False
    enforced: bool = False

    @property
    def active(self) -> bool:
        """The hold refuses spend only after ``credit_enforcement_at``."""

        return self.quarantined and self.enforced


@dataclass(frozen=True)
class OriginRequest:
    """A reversal debt whose consumption entries the adapter links."""

    debt_id: UUID
    lot_id: UUID
    amount_mc: int


@dataclass
class LedgerWrites:
    """Rows recorded by a book, in the order :meth:`CreditLedger.persist`
    inserts them."""

    effects: list[dict[str, Any]] = field(default_factory=list)
    lots: list[dict[str, Any]] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)
    debts: list[dict[str, Any]] = field(default_factory=list)
    origins: list[dict[str, Any]] = field(default_factory=list)
    origin_requests: list[OriginRequest] = field(default_factory=list)
    settlements: list[dict[str, Any]] = field(default_factory=list)
    # Effects the deadline sweep created (expiries of lots past their
    # deadline), as opposed to the effects of the operations themselves.
    swept_effect_ids: list[str] = field(default_factory=list)

    @property
    def effect_ids(self) -> tuple[str, ...]:
        """Every effect these rows create."""

        return tuple(str(row["effect_id"]) for row in self.effects)

    @property
    def operation_effect_ids(self) -> tuple[str, ...]:
        """The effects of the operations, without the sweep's expiries."""

        swept = set(self.swept_effect_ids)
        return tuple(effect for effect in self.effect_ids if effect not in swept)


@dataclass(frozen=True)
class GrantSpec:
    """The lot a settled credit-bearing line grants."""

    lot_kind: LotKind
    effect_id: str
    payment_id: str
    paid_at: date
    granted_mc: int
    expires_at: datetime
    subscription_id: UUID | None = None
    cycle_start: date | None = None
    cycle_end: date | None = None
    order_id: UUID | None = None


@dataclass(frozen=True)
class DebitResult:
    """``at`` is the instant the debit acted at -- the caller's ``at`` or the
    builder's later ledger clock.  Group 3 checks against it whatever depends
    on time (the monthly allowance only from its ``cycle_start``, O5)."""

    status: DebitStatus
    debited_mc: int = 0
    debt_mc: int = 0
    at: datetime | None = None


@dataclass(frozen=True)
class LineApplication:
    """What a set of cumulative targets did to one line."""

    reversal_delta_mc: int = 0
    restoration_delta_mc: int = 0
    reversal_regressed: bool = False
    restoration_regressed: bool = False
    restoration_capped: bool = False


class LedgerBook:
    """One builder's ledger under its account lock (no I/O)."""

    def __init__(
        self,
        *,
        builder_id: UUID,
        account_id: UUID,
        lots: Iterable[LotState] = (),
        debts: Iterable[DebtState] = (),
        known_effects: Iterable[str] = (),
        released_open: Mapping[UUID, int] | None = None,
        returned: Mapping[UUID, Mapping[UUID, int]] | None = None,
        spend_hold: SpendHold = SpendHold(),
        ledger_debt_mc: int | None = None,
        clock_floor: datetime | None = None,
        clock_ceiling: datetime | None = None,
    ) -> None:
        """``released_open`` is each debt's open part already released
        (``debt_release``), ``returned`` each debt's settled part already
        returned, per paying lot (``settlement_release``); see
        :func:`attribute_releases`.  ``clock_floor`` is the latest
        ``occurred_at`` already in the builder's ledger: no operation of this
        book acts before it; ``clock_ceiling`` (``database clock +
        tolerance`` when :class:`CreditLedger` opens the book): none acts
        after it."""

        self.builder_id = builder_id
        self.account_id = account_id
        self.spend_hold = spend_hold
        self.writes = LedgerWrites()
        self._clock: datetime | None = (
            None if clock_floor is None else _aware(clock_floor, "clock_floor")
        )
        self._ceiling: datetime | None = (
            None if clock_ceiling is None else _aware(clock_ceiling, "clock_ceiling")
        )
        if (
            self._clock is not None
            and self._ceiling is not None
            and self._clock > self._ceiling
        ):
            raise LedgerInvariantError("the book's clock is beyond its ceiling")
        self._lots: dict[UUID, LotState] = {lot.id: lot for lot in lots}
        self._debts: list[DebtState] = list(debts)
        self._sort_debts()
        known = list(known_effects)
        self._known_effects: set[str] = set(known)
        self._released_open: Counter[UUID] = Counter(released_open or {})
        self._returned: dict[UUID, Counter[UUID]] = {
            debt_id: Counter(by_lot) for debt_id, by_lot in (returned or {}).items()
        }
        debts_by_id = {debt.id: debt for debt in self._debts}
        for debt_id in set(self._released_open) | set(self._returned):
            debt = debts_by_id.get(debt_id)
            if debt is None or debt.original_lot_id is None:
                raise LedgerInvariantError("a release of no reversal debt")
            for payer, amount in self._returned.get(debt_id, Counter()).items():
                if not 0 <= amount <= debt.settled_by_lot[payer]:
                    raise LedgerInvariantError(
                        "a debt returned more than its payer settled"
                    )
        self._applied: dict[str, Counter[UUID]] = {
            REVERSAL_EFFECT: Counter(),
            RESTORATION_EFFECT: Counter(),
        }
        # The largest applied target wins, so the order does not matter.
        for effect_id in known:
            parsed = parse_line_effect_id(effect_id)
            if parsed is not None:
                kind, lot_id, target = parsed
                applied = self._applied[kind]
                # Targets only grow: the largest applied one is the current.
                applied[lot_id] = max(applied[lot_id], target)
        open_total = sum(self._open_by_debt().values())
        if ledger_debt_mc is not None and ledger_debt_mc != open_total:
            raise LedgerInvariantError(
                "open debt rows disagree with the ledger's derived debt"
            )

    def _sort_debts(self) -> None:
        def key(debt: DebtState) -> tuple[datetime, str, int, str]:
            line = (
                self._lots.get(debt.original_lot_id) if debt.original_lot_id else None
            )
            return debt.order_key(line.grant_effect_id if line else "")

        self._debts.sort(key=key)

    # -- the book's clock -----------------------------------------------------

    @property
    def clock(self) -> datetime | None:
        """The latest instant this book has acted at (or was loaded with)."""

        return self._clock

    def _tick(self, at: datetime) -> datetime:
        """The instant an operation acts at: ``at``, never before the clock
        nor after the ceiling."""

        at = self._read(at)
        self._clock = at
        return at

    def _read(self, at: datetime) -> datetime:
        at = _aware(at, "at")
        if self._ceiling is not None and at > self._ceiling:
            at = self._ceiling
        if self._clock is not None and at < self._clock:
            return self._clock
        return at

    # -- reads -------------------------------------------------------------

    @property
    def lots(self) -> tuple[LotState, ...]:
        return tuple(sorted(self._lots.values(), key=LotState.fefo_key))

    @property
    def debts(self) -> tuple[DebtState, ...]:
        return tuple(self._debts)

    def lot(self, lot_id: UUID) -> LotState:
        return self._lots[lot_id]

    def has_effect(self, effect_id: str) -> bool:
        return effect_id in self._known_effects

    def monthly_lot(
        self, subscription_id: UUID, cycle_start: date, cycle_end: date
    ) -> LotState | None:
        for lot in self._lots.values():
            if (
                lot.lot_kind == "monthly"
                and lot.subscription_id == subscription_id
                and lot.cycle_start == cycle_start
                and lot.cycle_end == cycle_end
            ):
                return lot
        return None

    def topup_lot(self, order_id: UUID) -> LotState | None:
        for lot in self._lots.values():
            if lot.lot_kind == "topup" and lot.order_id == order_id:
                return lot
        return None

    def applied_reversal_mc(self, lot_id: UUID) -> int:
        return self._applied[REVERSAL_EFFECT][lot_id]

    def applied_restoration_mc(self, lot_id: UUID) -> int:
        return self._applied[RESTORATION_EFFECT][lot_id]

    def open_debt_by_id(self) -> dict[UUID, int]:
        return self._open_by_debt()

    @property
    def open_debt_mc(self) -> int:
        return sum(self._open_by_debt().values())

    def available_mc(self, at: datetime) -> int:
        """Usable capacity at ``at`` (the APIs' and the UI's figure): what
        debits may spend, so a monthly allowance counts only from its
        ``cycle_start`` (O5, :meth:`LotState.spendable_mc`)."""

        at = self._read(at)
        return sum(lot.spendable_mc(at) for lot in self._lots.values())

    def available_trace_mc(self, at: datetime) -> int:
        """Usable capacity net of open debt (reservations: Group 4a)."""

        return max(self.available_mc(at) - self.open_debt_mc, 0)

    def reversal_debt_net_mc(self, lot_id: UUID) -> int:
        """This line's reversal debt still in force: open plus settled, net
        of what restorations released."""

        return sum(
            self._in_force(debt)
            for debt in self._debts
            if debt.original_lot_id == lot_id
        )

    def _in_force(self, debt: DebtState) -> int:
        returned = sum(self._returned.get(debt.id, Counter()).values())
        return debt.amount_mc - self._released_open[debt.id] - returned

    # -- operations --------------------------------------------------------

    def sweep_expired(self, at: datetime) -> None:
        """Append the ``expiry`` of every lot past its deadline with balance,
        after the start catch-up (an allowance pays what it owed from its
        start before it expires)."""

        at = self._tick(at)
        self._settle_started_allowances(at)
        before = len(self.writes.effects)
        for lot in self.lots:
            self._expire_if_due(lot, at)
        self.writes.swept_effect_ids.extend(
            str(row["effect_id"]) for row in self.writes.effects[before:]
        )

    def grant(
        self,
        spec: GrantSpec,
        *,
        at: datetime,
        decision_digest: str | None = None,
    ) -> LotState:
        """Create the lot of a settled line once, then settle open debt."""

        at = self._tick(at)
        if spec.granted_mc <= 0:
            raise ValueError("granted_mc must be positive")
        if spec.effect_id in self._known_effects:
            raise LedgerInvariantError("grant effect already exists")
        self._settle_started_allowances(at)
        self._ensure_effect(
            spec.effect_id, GRANT_EFFECT, decision_digest=decision_digest
        )
        lot = LotState(
            id=uuid4(),
            lot_kind=spec.lot_kind,
            paid_at=spec.paid_at,
            expires_at=_aware(spec.expires_at, "expires_at"),
            granted_mc=spec.granted_mc,
            grant_effect_id=spec.effect_id,
            payment_id=spec.payment_id,
            subscription_id=spec.subscription_id,
            cycle_start=spec.cycle_start,
            cycle_end=spec.cycle_end,
            order_id=spec.order_id,
        )
        self._lots[lot.id] = lot
        self.writes.lots.append(
            {
                "id": lot.id,
                "builder_id": self.builder_id,
                "account_id": self.account_id,
                "lot_kind": lot.lot_kind,
                "subscription_id": lot.subscription_id,
                "cycle_start": lot.cycle_start,
                "cycle_end": lot.cycle_end,
                "order_id": lot.order_id,
                "payment_id": lot.payment_id,
                "paid_at": lot.paid_at,
                "granted_mc": lot.granted_mc,
                "expires_at": lot.expires_at,
                "grant_effect_id": lot.grant_effect_id,
            }
        )
        self._entry(spec.effect_id, lot.id, "grant", spec.granted_mc, at)
        self._expire_if_due(lot, at)
        self._settle(spec.effect_id, at)
        return lot

    def settle_open_debts(
        self,
        effect_id: str,
        *,
        at: datetime,
        effect_kind: str | None = None,
    ) -> int:
        """Pay open debt, oldest first, from usable capacity (FEFO).

        ``effect_kind`` creates ``effect_id`` only when something is settled
        (the cycle opening's own ``debt-settlement`` effect); otherwise the
        effect must already exist in this book.
        """

        at = self._tick(at)
        self._settle_started_allowances(at)
        return self._settle(effect_id, at, effect_kind=effect_kind)

    def _settle_started_allowances(self, at: datetime) -> None:
        """The start catch-up (module docstring): for each monthly allowance
        whose spend start is not after ``at``, earliest start first (ties by
        :meth:`LotState.fefo_key`), settle the debt that existed at that
        start from the capacity spendable then, dated at the start.

        It runs under the account lock on every operation, so it skips --
        without changing any row -- every start where :meth:`_settle` would
        write nothing: the open debts are read once (none open: no start
        settles anything); a start before the oldest open debt has no debt
        it may pay (``debts_until``), and settling only closes debt, so that
        oldest one never gets older during the pass; a start where no lot is
        spendable has no payer -- and only a lot holding a positive balance
        can be spendable, settling only lowers balances, so the holders are
        read once (none: nothing can pay at any start); once the pass closed
        every debt open at its beginning (in millicredits), nothing is left
        to settle.  So a book with nothing open, or nothing left to pay
        with, costs at most one pass over its debts, whatever its history of
        allowances.
        """

        holders = [lot for lot in self._lots.values() if lot.balance_mc > 0]
        if not holders:
            return
        open_amounts = self._open_by_debt()
        open_left = sum(open_amounts.values())
        if open_left == 0:
            return
        oldest_open = min(
            debt.created_at for debt in self._debts if open_amounts[debt.id] > 0
        )
        started: list[tuple[datetime, LotState]] = []
        for lot in self._lots.values():
            starts = lot.spend_starts_at()
            if starts is not None and oldest_open <= starts <= at:
                started.append((starts, lot))
        started.sort(key=lambda pair: (pair[0], pair[1].fefo_key()))
        for starts, lot in started:
            if open_left == 0:
                break
            if (
                lot.subscription_id is None
                or lot.cycle_start is None
                or lot.cycle_end is None
            ):
                raise LedgerInvariantError("a monthly allowance names no cycle")
            if not any(payer.spendable_mc(starts) > 0 for payer in holders):
                continue
            effect_id = cycle_start_settlement_effect_id(
                lot.subscription_id, lot.cycle_start, lot.cycle_end
            )
            created = effect_id not in self._known_effects
            settled = self._settle(
                effect_id,
                starts,
                effect_kind=DEBT_SETTLEMENT_EFFECT,
                debts_until=starts,
            )
            open_left -= settled
            if settled and created:
                # Time-driven, like the sweep's expiries: not the effect of
                # the fact or debit this operation applies.
                self.writes.swept_effect_ids.append(effect_id)

    def _settle(
        self,
        effect_id: str,
        at: datetime,
        *,
        effect_kind: str | None = None,
        debts_until: datetime | None = None,
    ) -> int:
        """Settle open debt (created at or before ``debts_until``, when
        given) from what is spendable at ``at``, the entries dated ``at``.

        ``debts_until`` is defence in depth: through the book's operations a
        start ``s`` never sees open debt older than ``s`` next to capacity
        spendable at ``s`` once caught up, so with the catch-up's
        oldest-open-debt filter the guard decides nothing on histories this
        module wrote; it does on a book loaded from rows that break that
        (``test_a_loaded_book_pays_only_the_debt_that_existed_at_the_start``
        pins it), where a start must never pay a debt created after it."""

        open_amounts = self._open_by_debt()
        pending = [
            debt
            for debt in self._debts
            if open_amounts[debt.id] > 0
            and (debts_until is None or debt.created_at <= debts_until)
        ]
        # O5: a monthly allowance not yet started pays nothing.
        payers = [lot for lot in self.lots if lot.spendable_mc(at) > 0]
        if not pending or not payers:
            return 0
        if effect_kind is not None:
            self._ensure_effect(effect_id, effect_kind)
        elif effect_id not in self._known_effects:
            raise LedgerInvariantError("settlement needs an existing effect")
        settled = 0
        for debt in pending:
            remaining = open_amounts[debt.id]
            for lot in payers:
                if remaining == 0:
                    break
                amount = min(remaining, lot.spendable_mc(at))
                if amount <= 0:
                    continue
                self._entry(effect_id, lot.id, "debt_settlement", amount, at)
                self.writes.settlements.append(
                    {
                        "id": uuid4(),
                        "builder_id": self.builder_id,
                        "debt_id": debt.id,
                        "settling_grant_lot_id": lot.id,
                        "amount_mc": amount,
                        "effect_id": effect_id,
                    }
                )
                debt.settled_by_lot[lot.id] += amount
                remaining -= amount
                settled += amount
        return settled

    def apply_line_targets(
        self,
        lot_id: UUID,
        *,
        line_id: str,
        payment_id: str,
        financial_revision: int,
        reversal_target_mc: int,
        restoration_target_mc: int,
        at: datetime,
        decision_digest: str | None = None,
    ) -> LineApplication:
        """Bring one line to its cumulative reversal/restoration targets.

        Only the missing delta is applied; a lower target (older revision)
        changes nothing and is reported as a regression.
        """

        at = self._tick(at)
        lot = self._lots[lot_id]
        if not 0 <= reversal_target_mc <= lot.granted_mc:
            raise ValueError("reversal target outside [0, granted_mc]")
        if restoration_target_mc < 0:
            raise ValueError("restoration target must not be negative")
        self._settle_started_allowances(at)
        applied_reversal = self._applied[REVERSAL_EFFECT][lot_id]
        applied_restoration = self._applied[RESTORATION_EFFECT][lot_id]
        reversal_delta = restoration_delta = 0
        if reversal_target_mc > applied_reversal:
            reversal_delta = reversal_target_mc - applied_reversal
            effect_id = reversal_effect_id(
                lot_id,
                line_id=line_id,
                payment_id=payment_id,
                financial_revision=financial_revision,
                target_mc=reversal_target_mc,
            )
            self._ensure_effect(
                effect_id, REVERSAL_EFFECT, decision_digest=decision_digest
            )
            self._reverse(lot, reversal_delta, effect_id, at)
            self._applied[REVERSAL_EFFECT][lot_id] = reversal_target_mc
        capped = restoration_target_mc > self._applied[REVERSAL_EFFECT][lot_id]
        restoration_target = min(
            restoration_target_mc, self._applied[REVERSAL_EFFECT][lot_id]
        )
        if restoration_target > applied_restoration:
            restoration_delta = restoration_target - applied_restoration
            effect_id = restoration_effect_id(
                lot_id,
                line_id=line_id,
                payment_id=payment_id,
                financial_revision=financial_revision,
                target_mc=restoration_target,
            )
            self._ensure_effect(
                effect_id, RESTORATION_EFFECT, decision_digest=decision_digest
            )
            self._restore(lot, restoration_delta, effect_id, at)
            self._applied[RESTORATION_EFFECT][lot_id] = restoration_target
        return LineApplication(
            reversal_delta_mc=reversal_delta,
            restoration_delta_mc=restoration_delta,
            reversal_regressed=reversal_target_mc < applied_reversal,
            restoration_regressed=restoration_target < applied_restoration,
            restoration_capped=capped,
        )

    def debit(
        self,
        effect_id: str,
        amount_mc: int,
        *,
        at: datetime,
        effect_kind: str,
        tenant_id: UUID | None = None,
        decision_digest: str | None = None,
    ) -> DebitResult:
        """Consume ``amount_mc`` monthly-first then FEFO; the rest is debt.

        A known effect is a replay (nothing changes); an active quarantine
        hold answers ``credit_pending`` (nothing changes).
        """

        at = self._tick(at)
        if type(amount_mc) is not int or amount_mc <= 0:
            raise ValueError("amount_mc must be a positive integer")
        if effect_id in self._known_effects:
            return DebitResult("duplicate", at=at)
        if self.spend_hold.active:
            return DebitResult("credit_pending", at=at)
        # A refused or replayed debit changes nothing; an accepted one first
        # catches the started allowances up, then settles, then consumes.
        self._settle_started_allowances(at)
        self._ensure_effect(
            effect_id,
            effect_kind,
            tenant_id=tenant_id,
            decision_digest=decision_digest,
        )
        self._settle(effect_id, at)
        remaining = amount_mc
        for lot in self.lots:
            if remaining == 0:
                break
            # O5, against the instant the debit acts at (``DebitResult.at``).
            take = min(remaining, lot.spendable_mc(at))
            if take > 0:
                self._entry(effect_id, lot.id, "debit", take, at)
                remaining -= take
        if remaining:
            self._new_debt(
                effect_id=effect_id,
                source_kind=CONSUMPTION_DEBT,
                amount_mc=remaining,
                original_lot_id=None,
                at=at,
            )
        return DebitResult("applied", amount_mc - remaining, remaining, at=at)

    # -- internals ---------------------------------------------------------

    def _ensure_effect(
        self,
        effect_id: str,
        effect_kind: str,
        *,
        tenant_id: UUID | None = None,
        decision_digest: str | None = None,
    ) -> bool:
        if effect_id in self._known_effects:
            return False
        self._known_effects.add(effect_id)
        self.writes.effects.append(
            {
                "id": uuid4(),
                "builder_id": self.builder_id,
                "tenant_id": tenant_id,
                "effect_id": effect_id,
                "effect_kind": effect_kind,
                "decision_digest": decision_digest,
            }
        )
        return True

    def _entry(
        self,
        effect_id: str,
        lot_id: UUID | None,
        entry_type: str,
        amount_mc: int,
        occurred_at: datetime,
    ) -> UUID:
        if amount_mc <= 0:
            raise LedgerInvariantError("ledger amounts are positive")
        entry_id = uuid4()
        self.writes.entries.append(
            {
                "id": entry_id,
                "builder_id": self.builder_id,
                "account_id": self.account_id,
                "effect_id": effect_id,
                "lot_id": lot_id,
                "entry_type": entry_type,
                "amount_mc": amount_mc,
                "occurred_at": occurred_at,
            }
        )
        if lot_id is not None:
            self._lots[lot_id].sums[entry_type] += amount_mc
        return entry_id

    def _expire_if_due(self, lot: LotState, at: datetime) -> None:
        balance = lot.balance_mc
        if not lot.expired(at) or balance <= 0:
            return
        effect_id = expiry_effect_id(lot.id)
        if not self._ensure_effect(effect_id, EXPIRY_EFFECT):
            raise LedgerInvariantError("an expired lot regained usable balance")
        self._entry(effect_id, lot.id, "expiry", balance, lot.expires_at)

    def _new_debt(
        self,
        *,
        effect_id: str,
        source_kind: str,
        amount_mc: int,
        original_lot_id: UUID | None,
        at: datetime,
    ) -> DebtState:
        entry_id = self._entry(effect_id, None, "debt", amount_mc, at)
        debt = DebtState(
            id=uuid4(),
            created_at=at,
            source_kind=source_kind,
            source_effect_id=effect_id,
            original_lot_id=original_lot_id,
            amount_mc=amount_mc,
        )
        self._debts.append(debt)
        self._sort_debts()
        self.writes.debts.append(
            {
                "id": debt.id,
                "builder_id": self.builder_id,
                "account_id": self.account_id,
                "debt_source_kind": source_kind,
                "debt_source_effect_id": effect_id,
                "original_grant_lot_id": original_lot_id,
                "amount_mc": amount_mc,
                "created_at": at,
            }
        )
        if original_lot_id is None:
            # The shortfall's own ``debt`` entry is the consumption it records.
            self.writes.origins.append(
                {
                    "builder_id": self.builder_id,
                    "debt_id": debt.id,
                    "consumption_entry_id": entry_id,
                }
            )
        else:
            self.writes.origin_requests.append(
                OriginRequest(debt.id, original_lot_id, amount_mc)
            )
        return debt

    def _open_by_debt(self) -> dict[UUID, int]:
        """Open amount of every debt: its amount less what was settled and
        what was released of its open part (both name the debt)."""

        open_amounts: dict[UUID, int] = {}
        for debt in self._debts:
            open_amount = (
                debt.amount_mc - debt.settled_mc - self._released_open[debt.id]
            )
            if open_amount < 0:
                raise LedgerInvariantError(
                    "a debt was settled or released beyond its amount"
                )
            open_amounts[debt.id] = open_amount
        return open_amounts

    def _reverse(self, lot: LotState, delta: int, effect_id: str, at: datetime) -> None:
        net_before = (
            self._applied[REVERSAL_EFFECT][lot.id]
            - self._applied[RESTORATION_EFFECT][lot.id]
        )
        debt_net = self.reversal_debt_net_mc(lot.id)
        lot_side = net_before - debt_net
        # Unused capacity of the line that no reversal claims yet: what is
        # still available plus, on an expired lot, what expired unused.
        unclaimed = (lot.granted_mc - lot.consumed_mc) - lot_side
        withdrawn = min(delta, lot.available_mc(at))
        if withdrawn:
            self._entry(effect_id, lot.id, "reversal", withdrawn, at)
        absorbed = min(delta - withdrawn, max(unclaimed - withdrawn, 0))
        owed = delta - withdrawn - absorbed
        if owed:
            if owed > lot.consumed_mc - debt_net:
                raise LedgerInvariantError("reversal would owe more than consumed")
            self._new_debt(
                effect_id=effect_id,
                source_kind=REVERSAL_DEBT,
                amount_mc=owed,
                original_lot_id=lot.id,
                at=at,
            )
            self._settle(effect_id, at)

    def _closed(self, lot: LotState, at: datetime) -> bool:
        """Expired at ``at``, or already given its ``expiry`` by a writer whose
        clock was ahead: a restoration never makes such a lot usable again."""

        return lot.expired(at) or expiry_effect_id(lot.id) in self._known_effects

    def _restore(self, lot: LotState, delta: int, effect_id: str, at: datetime) -> None:
        net_before = (
            self._applied[REVERSAL_EFFECT][lot.id]
            - self._applied[RESTORATION_EFFECT][lot.id]
        )
        debt_in_force = self.reversal_debt_net_mc(lot.id)
        lot_side = net_before - debt_in_force
        released = self._release_line_debt(
            lot, min(delta, debt_in_force), effect_id, at
        )
        left = delta - released
        if left:
            if left > lot_side:
                raise LedgerInvariantError("restoration exceeds what was reversed")
            self._entry(effect_id, lot.id, "lot_regrant", left, at)
            if self._closed(lot, at):
                self._entry(effect_id, lot.id, "expiry", left, at)
        self._settle(effect_id, at)

    def _release_line_debt(
        self, line: LotState, amount: int, effect_id: str, at: datetime
    ) -> int:
        """Release ``amount`` of ``line``'s reversal debt in force, set off by
        ``effect_id`` (a restoration of the line, or a release of it as a
        payer).

        Newest debt first (the cumulative target orders a line's debts): a
        restoration undoes the latest reversal first, so a reversal won at
        once moves no capacity between lots.  Of each debt, its open part
        first (``debt_release``), then its settled part back to exactly the
        lots that paid THAT debt, latest-expiring first
        (``settlement_release``, see :meth:`_credit_payer`).  Each debt's
        entries hang from its own ``return:{debt_id}:{digest}`` effect.
        """

        if amount <= 0:
            return 0
        open_amounts = self._open_by_debt()
        left = amount
        for debt in sorted(
            (d for d in self._debts if d.original_lot_id == line.id),
            key=lambda d: (d.target_mc, d.created_at, d.source_effect_id),
            reverse=True,
        ):
            if left == 0:
                break
            if self._in_force(debt) <= 0:
                continue
            released_open = min(left, open_amounts[debt.id])
            left -= released_open
            returned = self._returned.setdefault(debt.id, Counter())
            shares: list[tuple[LotState, int]] = []
            for payer in sorted(
                (
                    self._lots[lot_id]
                    for lot_id, settled in debt.settled_by_lot.items()
                    if settled - returned[lot_id] > 0
                ),
                key=LotState.fefo_key,
                reverse=True,
            ):
                if left == 0:
                    break
                share = min(left, debt.settled_by_lot[payer.id] - returned[payer.id])
                shares.append((payer, share))
                left -= share
            if not released_open and not shares:
                continue
            release = return_effect_id(debt.id, effect_id)
            if not self._ensure_effect(release, RETURN_EFFECT):
                raise LedgerInvariantError("a debt was released twice by one effect")
            if released_open:
                self._entry(release, None, "debt_release", released_open, at)
                self._released_open[debt.id] += released_open
            for payer, share in shares:
                returned[payer.id] += share
                self._credit_payer(payer, share, release, at)
        if left:
            raise LedgerInvariantError("released more than the line owed")
        return amount

    def _credit_payer(
        self, payer: LotState, amount: int, effect_id: str, at: datetime
    ) -> None:
        """Return to ``payer`` what it had settled of a released debt.

        If the payer was itself charged back, the returned amount first
        cancels the payer's own reversal debt in force (owner decisions,
        option (a) and E): that part is not capacity of the payer (its
        funding was refunded), so the payer withdraws it again (``reversal``)
        and releases as much of its own debt, which cascades to whoever paid
        that debt.  Only what exceeds the payer's debt in force is capacity
        of the payer -- with an equal ``expiry`` when the payer is closed.
        So no line ever holds usable capacity while its own reversal debt is
        in force.
        """

        self._entry(effect_id, payer.id, "settlement_release", amount, at)
        # The returned payment first cancels the payer's reversal debt in
        # force (owner decision E): a charged-back payer never keeps capacity
        # while its own reversal debt stands; what it owed goes back to the
        # lots that really paid it.
        excess = min(amount, max(self.reversal_debt_net_mc(payer.id), 0))
        if excess:
            release = release_effect_id(payer.id, effect_id)
            if not self._ensure_effect(release, RELEASE_EFFECT):
                raise LedgerInvariantError("a release effect was recorded twice")
            self._entry(release, payer.id, "reversal", excess, at)
            self._release_line_debt(payer, excess, release, at)
        kept = amount - excess
        if kept and self._closed(payer, at):
            self._entry(effect_id, payer.id, "expiry", kept, at)


# ---------------------------------------------------------------------------
# Loading a book from its stored rows (pure: the adapter and the property
# tests share it).
# ---------------------------------------------------------------------------


def attribute_releases(
    release_rows: Iterable[tuple[str, str, UUID | None, int]],
) -> tuple[Counter[UUID], dict[UUID, Counter[UUID]]]:
    """Releases per debt: ``debt_release`` totals (its open part), and
    ``settlement_release`` totals per paying lot (its settled part).  The
    debt is the one the entry's ``return:`` effect names
    (:func:`returned_debt`); the payer is the lot the entry names.  A release
    under any other effect is an invariant error."""

    released_open: Counter[UUID] = Counter()
    returned: dict[UUID, Counter[UUID]] = {}
    for effect_id, entry_type, lot_id, amount in release_rows:
        debt_id = returned_debt(effect_id)
        if debt_id is None:
            raise LedgerInvariantError("a release outside a return effect")
        if entry_type == "debt_release":
            released_open[debt_id] += amount
        elif entry_type == "settlement_release" and lot_id is not None:
            returned.setdefault(debt_id, Counter())[lot_id] += amount
        else:
            raise LedgerInvariantError("an unexpected release entry")
    return released_open, returned


def assemble_book(
    *,
    builder_id: UUID,
    account_id: UUID,
    lots: Iterable[Mapping[str, Any]],
    lot_sums: Mapping[UUID, Mapping[str, int]],
    debts: Iterable[Mapping[str, Any]],
    settled: Mapping[UUID, Mapping[UUID, int]],
    known_effects: Iterable[str],
    release_rows: Iterable[tuple[str, str, UUID | None, int]],
    ledger_debt_mc: int,
    clock_floor: datetime | None,
    clock_ceiling: datetime | None = None,
    spend_hold: SpendHold = SpendHold(),
) -> LedgerBook:
    """A builder's book from its stored rows (``credit_lots`` and
    ``credit_debts`` columns, per-lot entry sums, per-debt settlement sums,
    effect ids, release entry sums).  :meth:`CreditLedger.open_book` loads
    with it; a reloaded book behaves exactly like one kept in memory."""

    released_open, returned = attribute_releases(release_rows)
    return LedgerBook(
        builder_id=builder_id,
        account_id=account_id,
        lots=[
            LotState(
                id=lot["id"],
                lot_kind=lot["lot_kind"],
                paid_at=lot["paid_at"],
                expires_at=_aware(_utc(lot["expires_at"]), "expires_at"),
                granted_mc=int(lot["granted_mc"]),
                grant_effect_id=lot["grant_effect_id"],
                payment_id=lot["payment_id"],
                subscription_id=lot["subscription_id"],
                cycle_start=lot["cycle_start"],
                cycle_end=lot["cycle_end"],
                order_id=lot["order_id"],
                sums=Counter(lot_sums.get(lot["id"], {})),
            )
            for lot in lots
        ],
        debts=[
            DebtState(
                id=debt["id"],
                created_at=_utc(debt["created_at"]),
                source_kind=debt["debt_source_kind"],
                source_effect_id=debt["debt_source_effect_id"],
                original_lot_id=debt["original_grant_lot_id"],
                amount_mc=int(debt["amount_mc"]),
                settled_by_lot=Counter(settled.get(debt["id"], {})),
            )
            for debt in debts
        ],
        known_effects=sorted(known_effects),
        released_open=released_open,
        returned=returned,
        spend_hold=spend_hold,
        ledger_debt_mc=ledger_debt_mc,
        clock_floor=None if clock_floor is None else _utc(clock_floor),
        clock_ceiling=clock_ceiling,
    )


_LOT_COLUMNS: Final = (
    "id",
    "lot_kind",
    "paid_at",
    "expires_at",
    "granted_mc",
    "grant_effect_id",
    "payment_id",
    "subscription_id",
    "cycle_start",
    "cycle_end",
    "order_id",
)
_DEBT_COLUMNS: Final = (
    "id",
    "created_at",
    "debt_source_kind",
    "debt_source_effect_id",
    "original_grant_lot_id",
    "amount_mc",
)


# ---------------------------------------------------------------------------
# Database adapter.
# ---------------------------------------------------------------------------


class CreditLedger:
    """Loads a :class:`LedgerBook` under the account lock and persists it.

    Runs in the caller's transaction; never commits or rolls back.

    Clocks are checked against the DATABASE clock (PostgreSQL
    ``clock_timestamp()``), for any caller (the fact consumer, Group 3's
    debits ...).  :meth:`checked_instant` never refuses a writer for its own
    instant (owner decision C): an ``at`` more than the tolerance ahead acts
    at ``database clock + tolerance``, with a warning -- the book's clock
    never goes back, so a writer acting further in the future would expire
    lots early for every later writer, irreparably -- and an ``at`` more than
    the tolerance behind acts at ``database clock - tolerance`` (owner
    decision A).  ``open_book`` reads the database clock once the account
    lock is held and every operation of its book acts within that bound,
    whatever ``at`` it is given.  Its one refusal (:class:`LedgerClockAhead`,
    with a warning; the caller's transaction, with the account row the lock
    step may have inserted, rolls back) is a builder whose ledger clock (its
    latest ``occurred_at``) is already beyond ``database clock + tolerance``
    -- the database clock stepped back: its writers are refused until the
    database clock catches up, then the builder works again; nothing is
    repaired.  The tolerance is
    the setting ``credit_clock_tolerance_seconds`` (1..600, default 300);
    ``clock_tolerance`` overrides it and ``reference_clock`` replaces the
    database clock (tests).
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        clock_tolerance: timedelta | None = None,
        reference_clock: Callable[[], Awaitable[datetime]] | None = None,
    ) -> None:
        if clock_tolerance is None:
            clock_tolerance = configured_clock_tolerance()
        if not MIN_CLOCK_TOLERANCE <= clock_tolerance <= MAX_CLOCK_TOLERANCE:
            raise ValueError(
                "clock_tolerance must be within "
                f"[{MIN_CLOCK_TOLERANCE}, {MAX_CLOCK_TOLERANCE}]"
            )
        self.session = session
        self.repository = CreditRepository(session)
        self.clock_tolerance = clock_tolerance
        self._reference_clock = reference_clock

    async def reference_now(self) -> datetime:
        """The clock writers are checked against (the database's)."""

        if self._reference_clock is not None:
            return _aware(await self._reference_clock(), "reference clock")
        return await self.repository.database_now()

    async def checked_instant(self, builder_id: UUID, at: datetime) -> datetime:
        """The instant a writer of ``builder_id`` acts at, within the
        tolerance of the database clock: more than the tolerance ahead is
        limited to ``database clock + tolerance``, with a warning (owner
        decision C); more than the tolerance behind is raised to ``database
        clock - tolerance`` (owner decision A).  Never refuses; the book's
        own clock may raise the instant further."""

        return self._within(builder_id, _aware(at, "at"), await self.reference_now())

    def _within(self, builder_id: UUID, at: datetime, reference: datetime) -> datetime:
        ceiling = reference + self.clock_tolerance
        if at > ceiling:
            logger.warning(
                "credit ledger limited a writer clock ahead of the database",
                builder_id=str(builder_id),
                seconds_ahead=int((at - reference).total_seconds()),
                tolerance_seconds=int(self.clock_tolerance.total_seconds()),
            )
            return ceiling
        floor = reference - self.clock_tolerance
        return floor if at < floor else at

    def _refuse_ledger_ahead(
        self, builder_id: UUID, latest: datetime, reference: datetime
    ) -> None:
        if latest > reference + self.clock_tolerance:
            logger.warning(
                "credit ledger refused a builder whose ledger clock is ahead "
                "of the database",
                builder_id=str(builder_id),
                seconds_ahead=int((latest - reference).total_seconds()),
                tolerance_seconds=int(self.clock_tolerance.total_seconds()),
            )
            raise LedgerClockAhead(
                "the ledger clock is ahead of the database clock by more than "
                f"{self.clock_tolerance}"
            )

    async def spend_hold(self, builder_id: UUID) -> SpendHold:
        """The builder's quarantine hold (read-only; exposed before the
        enforcement too, when it does not act)."""

        return SpendHold(
            quarantined=await self.repository.quarantine_hold(builder_id),
            enforced=await self.repository.enforcement_at() is not None,
        )

    async def open_book(
        self,
        builder_id: UUID,
        *,
        at: datetime,
        effect_ids: Iterable[str] = (),
    ) -> LedgerBook:
        """Lock order steps 2-3, then load the builder's state and sweep.

        ``effect_ids`` are extra ids (a debit's) whose existence the book
        must know; the ledger's own effect kinds are always loaded.  The
        book's clock starts at the latest ``occurred_at`` of the builder's
        ledger, so an ``at`` behind another writer's never acts in the past;
        every operation of the book acts within the tolerance of the
        database clock read once the lock is held (:meth:`checked_instant`).
        """

        at = _aware(at, "at")
        repository = self.repository
        account_id = await repository.ensure_account(builder_id)
        account = await repository.lock_account(builder_id)
        if account is None or account.id != account_id:
            raise LedgerInvariantError("the builder's account vanished")
        # Read only now: a reading taken before waiting on the lock could be
        # older than what the previous holder wrote.
        reference = await self.reference_now()
        latest = await repository.latest_occurred_at(builder_id)
        if latest is not None:
            # A ledger clock beyond the database clock + tolerance (the
            # database clock stepped back, a manual row): no writer may
            # build on it until the database clock catches up (fail closed;
            # the caller's transaction, with the account row the lock step
            # may have inserted, rolls back; nothing is repaired; no such
            # row exists on ``main``).
            self._refuse_ledger_ahead(builder_id, _utc(latest), reference)
        at = self._within(builder_id, at, reference)
        hold = await self.spend_hold(builder_id)
        locked = await repository.lock_lots(builder_id)
        known = set(
            await repository.effect_ids_of_kinds(builder_id, LEDGER_EFFECT_KINDS)
        )
        known |= await repository.existing_effect_ids(builder_id, effect_ids)
        book = assemble_book(
            builder_id=builder_id,
            account_id=account_id,
            lots=[
                {column: getattr(lot, column) for column in _LOT_COLUMNS}
                for lot in locked
            ],
            lot_sums=await repository.lot_entry_sums(builder_id),
            debts=[
                {column: getattr(debt, column) for column in _DEBT_COLUMNS}
                for debt in await repository.debts(builder_id)
            ],
            settled=await repository.settlement_sums(builder_id),
            known_effects=known,
            release_rows=await repository.release_sums(builder_id),
            ledger_debt_mc=(await repository.derived_balance(builder_id)).debt_mc,
            clock_floor=latest,
            clock_ceiling=reference + self.clock_tolerance,
            spend_hold=hold,
        )
        book.sweep_expired(at)
        return book

    async def persist(self, book: LedgerBook) -> tuple[str, ...]:
        """Insert the book's recorded rows; returns the new effect ids."""

        writes = book.writes
        for model, rows in (
            (CreditEffect, writes.effects),
            (CreditLot, writes.lots),
            (CreditLedgerEntry, writes.entries),
            (CreditDebt, writes.debts),
        ):
            if rows:
                await self.session.execute(insert(model), rows)
        if writes.origins:
            await self.session.execute(insert(CreditDebtOrigin), writes.origins)
        for request in writes.origin_requests:
            # One request at a time: the next one sees these links.
            origins = await self._origins_for(book, request)
            if origins:
                await self.session.execute(insert(CreditDebtOrigin), origins)
        if writes.settlements:
            await self.session.execute(insert(CreditDebtSettlement), writes.settlements)
        created = writes.effect_ids
        book.writes = LedgerWrites()
        return created

    async def _origins_for(
        self, book: LedgerBook, request: OriginRequest
    ) -> list[dict[str, Any]]:
        """Link a reversal debt to the latest consumption entries of its lot
        not yet linked to another debt of that lot, until they cover it."""

        entries = await self.repository.unlinked_consumption_entries(
            book.builder_id, request.lot_id
        )
        rows: list[dict[str, Any]] = []
        covered = 0
        for entry_id, amount in entries:
            rows.append(
                {
                    "builder_id": book.builder_id,
                    "debt_id": request.debt_id,
                    "consumption_entry_id": entry_id,
                }
            )
            covered += amount
            if covered >= request.amount_mc:
                break
        return rows

    async def debit(
        self,
        builder_id: UUID,
        effect_id: str,
        amount_mc: int,
        *,
        at: datetime,
        effect_kind: str,
        tenant_id: UUID | None = None,
        decision_digest: str | None = None,
    ) -> DebitResult:
        """Open the book, consume, persist (caller's transaction).

        A refused (``credit_pending``) or replayed debit persists nothing --
        not even the expiries the book's deadline sweep recorded: the ledger
        does not change (the next accepted writer sweeps instead).
        """

        book = await self.open_book(builder_id, at=at, effect_ids=(effect_id,))
        result = book.debit(
            effect_id,
            amount_mc,
            at=at,
            effect_kind=effect_kind,
            tenant_id=tenant_id,
            decision_digest=decision_digest,
        )
        if result.status == "applied":
            await self.persist(book)
        return result


def _utc(value: datetime) -> datetime:
    """SQLite drops the zone of TIMESTAMPTZ; its stored values are UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "CONSUMPTION_DEBT",
    "CREDIT_TIMEZONE",
    "DEBT_SETTLEMENT_EFFECT",
    "EXPIRY_EFFECT",
    "GRANT_EFFECT",
    "LEDGER_EFFECT_KINDS",
    "MILLICREDITS_PER_CREDIT",
    "RESTORATION_EFFECT",
    "REVERSAL_DEBT",
    "REVERSAL_EFFECT",
    "TOPUP_VALIDITY_MONTHS",
    "CreditLedger",
    "DebitResult",
    "DebtState",
    "GrantSpec",
    "LedgerBook",
    "LedgerInvariantError",
    "LedgerWrites",
    "LineApplication",
    "LotState",
    "OriginRequest",
    "SpendHold",
    "add_calendar_months",
    "civil_date",
    "cumulative_target_mc",
    "cycle_debt_settlement_effect_id",
    "cycle_start_settlement_effect_id",
    "expiry_effect_id",
    "monthly_grant_effect_id",
    "monthly_lot_expires_at",
    "parse_line_effect_id",
    "restoration_effect_id",
    "reversal_effect_id",
    "start_of_civil_day",
    "topup_grant_effect_id",
    "topup_lot_expires_at",
    "topup_lot_expires_on",
]
