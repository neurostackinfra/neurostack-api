"""Credit exhaustion lifecycle (neurostack-credit-system Group 5).

States (derived, per builder): ``active`` (no open episode), ``grace`` (an
open episode before its suspension deadline: service continues as debt),
``suspended`` (the ``suspended`` transition is recorded) and
``deletion_due`` (the ``deletion_due`` transition is recorded; content is
never deleted automatically).

Inert before the enforcement.  Nothing here exists before
``credit_enforcement_at``: :meth:`CreditLifecycle.evaluate` returns at once
while it is unset (or later than the instant evaluated), the worker claims
nothing, and no episode, threshold, transition or deadline is inherited at
the enforcement instant -- an episode only ever opens on an effect AFTER it.

Opening (owner decisions; DESIGN "esgotamento").  Evaluated under the
builder's account lock after every ledger write (:meth:`CreditLedger.persist`
calls :meth:`CreditLifecycle.evaluate`), so the first effect after the
enforcement that creates debt or finds zero usable capacity
(``LedgerBook.available_trace_mc == 0``) in an account without an open
episode opens one with ``exhausted_at`` = that effect's instant (its commit,
under the lock).  When the only rows written were the deadline sweep's
expiries (the worker, no operation), ``exhausted_at`` is the latest expiry
deadline (never before the enforcement).  A later zero, a repeated zero or a
partial debt never moves ``exhausted_at`` (one open episode per builder).

Deadlines (owner decisions H3-(i) and M2, Decision 11).  Every episode keeps
72 exact hours of service as debt: the suspension deadline is
``max(exhausted_at, grace_end) + 72 h`` when ``exhausted_at`` falls in
``[cycle_end, grace_end)`` of the builder's binding, and ``exhausted_at +
72 h`` otherwise (:func:`suspension_due`).  ``grace_end`` comes from the one
helper :func:`binding_grace_end` (Group 7 reuses it in the request):
``cycle_end + credit_binding_grace_days`` civil days, or earlier -- the
commit of the following cycle's binding, or the first instant the credit
observed the subscription ``is_active = false`` (first-write-wins, recorded
in the episode: ``billing_subscriptions`` keeps no deactivation instant);
an inactive subscription has no grace.  The recorded grace only SHORTENS.
Retention counts 30 civil days from ``exhausted_at`` in America/Sao_Paulo
(:func:`deletion_due`: same local wall-clock time, so a DST change never
moves it by an hour).  Owner notice (no formula change): exhausting just
before ``cycle_end`` gains nothing from the grace.

Transitions.  ``suspended`` at the suspension deadline and ``deletion_due``
at the retention deadline are recorded by whichever comes first -- the
periodic worker (:func:`run_lifecycle_once`, accounts claimed ``FOR UPDATE
SKIP LOCKED``) or the evaluation of a metered request under the account lock
(:func:`request_gate`) -- with the SAME effect id
(``lifecycle:{episode_id}:{transition}``) and the deadline as
``occurred_at``, so either writes the identical row exactly once.  Before
deciding to suspend, both first settle open debt (``settle_open_debts`` on a
swept book: an allowance paid early pays from its ``cycle_start``, never
before), and an allowance arriving at its start reactivates a suspended
builder even without traffic.

Reactivation.  An open episode closes (``reactivated_at``, transition
``reactivated``) only when open debt is zero AND usable capacity is
positive, from grace, suspended or deletion_due alike (content not yet
deleted).  It re-arms the thresholds: their arm key carries the number of
closed episodes.

Thresholds.  50/90/100 % of the usable capacity in force (spendable lots,
net of reversals) consumed, counting open debt (``used = capacity -
available_trace``); one alert per threshold per arm (closed-episode count
and accounting window), ``UNIQUE`` in the database.

Never reads ``tests/``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.config.settings import settings
from src.models.credit import CreditLedgerEntry
from src.repositories.content_retention import ContentRetentionRepository, as_utc
from src.services.credit_ledger import (
    CREDIT_TIMEZONE,
    DEBT_SETTLEMENT_EFFECT,
    LedgerBook,
    LedgerWrites,
    LotState,
    start_of_civil_day,
)
from src.services.credit_switch import run_credit_worker_tick

if TYPE_CHECKING:
    from src.models.content_retention import CreditLifecycleEpisode

logger = get_logger(__name__)

DEBT_GRACE: Final = timedelta(hours=72)
RETENTION_DAYS: Final = 30
THRESHOLDS: Final = (50, 90, 100)
LIFECYCLE_LOG: Final = "credit lifecycle"
WORKER_BATCH: Final = 50
_OPENED_EPISODES: Final = "credit_lifecycle_opened_episodes"

LifecycleState = Literal["active", "grace", "suspended", "deletion_due"]
LifecycleSource = Literal["effect", "worker"]
GateVerdict = Literal["ok", "suspended"]
CategoryVerdict = Literal["ok", "category_exhausted"]


# ---------------------------------------------------------------------------
# Pure time rules.
# ---------------------------------------------------------------------------


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def add_civil_days(instant: datetime, days: int) -> datetime:
    """The same America/Sao_Paulo wall-clock time ``days`` civil days later.

    Aware arithmetic on a ``ZoneInfo`` datetime is wall-clock arithmetic;
    the offset is recomputed for the target day (DST-safe)."""

    local = _aware(instant, "instant").astimezone(CREDIT_TIMEZONE)
    shifted = (local + timedelta(days=days)).replace(fold=0)
    return shifted.astimezone(timezone.utc)


def binding_grace_end(
    cycle_end: datetime,
    *,
    grace_days: int,
    is_active: bool,
    observed_inactive_at: datetime | None = None,
    next_binding_at: datetime | None = None,
) -> datetime:
    """The end of one binding's renewal grace -- it never extends.

    ``cycle_end`` is the instant the cycle ends (``start_of_civil_day`` of
    the binding's ``cycle_end``).  The grace is ``grace_days`` civil days
    after it, cut at the first of: the commit of the following cycle's
    binding, the first observed ``is_active = false``.  An inactive
    subscription with no recorded observation has no grace at all.  No cut
    falls before ``cycle_end``.
    """

    cycle_end = _aware(cycle_end, "cycle_end")
    if grace_days < 0:
        raise ValueError("grace_days must not be negative")
    if not is_active and observed_inactive_at is None:
        return cycle_end
    deadline = add_civil_days(cycle_end, grace_days)
    for candidate in (observed_inactive_at, next_binding_at):
        if candidate is not None:
            deadline = min(deadline, max(_aware(candidate, "cut"), cycle_end))
    return deadline


def suspension_due(
    exhausted_at: datetime,
    *,
    cycle_end: datetime | None = None,
    binding_grace_ends_at: datetime | None = None,
) -> datetime:
    """72 exact hours of debt, counted from the grace end only when
    ``exhausted_at`` falls inside ``[cycle_end, grace_end)``."""

    exhausted_at = _aware(exhausted_at, "exhausted_at")
    anchor = exhausted_at
    if cycle_end is not None and binding_grace_ends_at is not None:
        cycle_end = _aware(cycle_end, "cycle_end")
        grace_end = _aware(binding_grace_ends_at, "binding_grace_ends_at")
        if cycle_end <= exhausted_at < grace_end:
            anchor = max(exhausted_at, grace_end)
    return anchor + DEBT_GRACE


def deletion_due(exhausted_at: datetime) -> datetime:
    """Retention: the same local time 30 civil days after ``exhausted_at``."""

    return add_civil_days(exhausted_at, RETENTION_DAYS)


def lifecycle_state(
    *,
    open_episode: bool,
    suspended: bool,
    deletion_marked: bool,
) -> LifecycleState:
    if not open_episode:
        return "active"
    if deletion_marked:
        return "deletion_due"
    if suspended:
        return "suspended"
    return "grace"


# ---------------------------------------------------------------------------
# Pure capacity and threshold rules (on a swept book).
# ---------------------------------------------------------------------------


def _funded_mc(lot: LotState) -> int:
    """What a lot brought, net of what refunds withdrew from it."""

    return max(lot.sums["grant"] + lot.sums["lot_regrant"] - lot.sums["reversal"], 0)


def capacity_in_force_mc(book: LedgerBook, at: datetime) -> int:
    """Capacity of the lots usable at ``at`` (started, unexpired)."""

    total = 0
    for lot in book.lots:
        starts = lot.spend_starts_at()
        if lot.expired(at) or (starts is not None and at < starts):
            continue
        total += _funded_mc(lot)
    return total


def thresholds_reached(capacity_mc: int, available_mc: int) -> tuple[int, ...]:
    """The thresholds whose share of ``capacity_mc`` is consumed.

    ``available_mc`` is usable capacity net of open debt; zero is always
    100 %, so an account with no capacity at all reports exhaustion."""

    if available_mc <= 0:
        return THRESHOLDS
    if capacity_mc <= 0:
        return ()
    used = max(capacity_mc - available_mc, 0)
    return tuple(t for t in THRESHOLDS if used * 100 >= t * capacity_mc)


def arm_key(closed_episodes: int, window_start: str) -> str:
    return f"g{closed_episodes}:w{window_start}"


# ---------------------------------------------------------------------------
# Effect ids: the worker and the request write the same ones.
# ---------------------------------------------------------------------------


def opening_effect_id(builder_id: UUID, generation: int) -> str:
    return f"lifecycle:{builder_id}:episode:{generation}:opened"


def transition_effect_id(episode_id: UUID, transition: str) -> str:
    return f"lifecycle:{episode_id}:{transition}"


def grace_effect_id(episode_id: UUID, grace_end: datetime) -> str:
    return f"lifecycle:{episode_id}:grace:{_aware(grace_end, 'grace').isoformat()}"


def settlement_effect_id(builder_id: UUID, at: datetime) -> str:
    return f"lifecycle-settlement:{builder_id}:{_aware(at, 'at').isoformat()}"


def threshold_effect_id(builder_id: UUID, key: str, threshold: int) -> str:
    return f"threshold:{builder_id}:{key}:{threshold}"


# ---------------------------------------------------------------------------
# Evaluation under the account lock.
# ---------------------------------------------------------------------------


@dataclass
class LifecycleOutcome:
    """What one evaluation saw and wrote (``evaluated`` is False while inert)."""

    evaluated: bool = False
    state: LifecycleState = "active"
    episode_id: UUID | None = None
    transitions: list[str] = field(default_factory=list)
    thresholds: list[int] = field(default_factory=list)


_INERT: Final = LifecycleOutcome()


def _exhaustion_instant(
    book: LedgerBook, writes: LedgerWrites, at: datetime, enforcement: datetime
) -> datetime | None:
    """``exhausted_at`` of an episode this evaluation opens, or ``None``.

    An operation (debit, reservation, reversal, maintenance...) opens it at
    its own instant; the deadline sweep alone at the latest expiry deadline
    it recorded (never before the enforcement); nothing written at all is
    no effect -- an idle zero is never an exhaustion, nor inherited."""

    if writes.operation_effect_ids:
        return at
    deadline = _swept_expiry_deadline(book, writes)
    if deadline is None:
        return None
    return max(deadline, enforcement)


def _iso(value: datetime | None) -> str | None:
    stamp = as_utc(value)
    return None if stamp is None else stamp.isoformat()


def _swept_expiry_deadline(book: LedgerBook, writes: LedgerWrites) -> datetime | None:
    deadlines: list[datetime] = []
    for effect_id in writes.swept_effect_ids:
        if not effect_id.startswith("expiry:"):
            continue
        try:
            deadlines.append(book.lot(UUID(effect_id.split(":", 1)[1])).expires_at)
        except (KeyError, ValueError):
            continue
    return max(deadlines) if deadlines else None


class CreditLifecycle:
    """Lifecycle evaluation in the caller's transaction (never commits)."""

    def __init__(self, session: AsyncSession, *, grace_days: int | None = None) -> None:
        self.session = session
        self.repository = ContentRetentionRepository(session)
        self.grace_days = (
            settings.credit_binding_grace_days if grace_days is None else grace_days
        )

    async def enforcement_at(self) -> datetime | None:
        from src.repositories.credit import CreditRepository

        return as_utc(await CreditRepository(self.session).enforcement_at())

    async def evaluate(
        self,
        book: LedgerBook,
        *,
        source: LifecycleSource,
        writes: LedgerWrites | None = None,
        enforcement_at: datetime | None = None,
    ) -> LifecycleOutcome:
        """Open, shorten, transition, reactivate and alert -- AFTER the book's
        rows were inserted, under the account lock the book was opened with.

        Inert (writes nothing) while ``credit_enforcement_at`` is unset or
        after the instant evaluated."""

        at = book.clock
        if at is None:
            return _INERT
        enforcement = enforcement_at or await self.enforcement_at()
        if enforcement is None or at < enforcement:
            return _INERT
        outcome = LifecycleOutcome(evaluated=True)
        builder_id = book.builder_id
        available = book.available_trace_mc(at)
        debt = book.open_debt_mc
        episode = await self.repository.open_episode(builder_id)
        closed = await self.repository.closed_episode_count(builder_id)

        if episode is not None and debt == 0 and available > 0:
            await self._reactivate(episode, at, source, outcome)
            episode = None
            closed += 1
        elif episode is None and available == 0:
            exhausted_at = _exhaustion_instant(
                book, writes or LedgerWrites(), at, enforcement
            )
            if exhausted_at is not None:
                episode = await self._open(book, exhausted_at, closed, source, outcome)

        if episode is not None:
            await self._observe_grace(episode, at, source, outcome)
            recorded = await self.repository.transitions_of(episode.id)
            for transition, deadline in (
                ("suspended", as_utc(episode.suspension_due_at)),
                ("deletion_due", as_utc(episode.deletion_due_at)),
            ):
                assert deadline is not None
                if transition in recorded or at < deadline:
                    continue
                if await self.repository.insert_transition(
                    builder_id=builder_id,
                    episode_id=episode.id,
                    transition=transition,
                    effect_id=transition_effect_id(episode.id, transition),
                    occurred_at=deadline,
                    source=source,
                ):
                    outcome.transitions.append(transition)
                recorded[transition] = deadline
            outcome.episode_id = episode.id
            outcome.state = lifecycle_state(
                open_episode=True,
                suspended="suspended" in recorded,
                deletion_marked="deletion_due" in recorded,
            )
        await self._thresholds(book, at, closed, available, outcome)
        if outcome.transitions or outcome.thresholds:
            logger.info(
                LIFECYCLE_LOG,
                builder_id=str(builder_id),
                state=outcome.state,
                transitions=outcome.transitions,
                thresholds=outcome.thresholds,
                source=source,
            )
        return outcome

    async def _open(
        self,
        book: LedgerBook,
        exhausted_at: datetime,
        generation: int,
        source: LifecycleSource,
        outcome: LifecycleOutcome,
    ) -> CreditLifecycleEpisode:
        cycle_end_at, grace_end, observed, next_binding = await self._grace_inputs(
            book.builder_id, exhausted_at
        )
        due = suspension_due(
            exhausted_at, cycle_end=cycle_end_at, binding_grace_ends_at=grace_end
        )
        effect_id = opening_effect_id(book.builder_id, generation)
        episode = await self.repository.insert_episode(
            builder_id=book.builder_id,
            account_id=book.account_id,
            opened_effect_id=effect_id,
            exhausted_at=exhausted_at,
            cycle_end_at=cycle_end_at,
            grace_ends_at=grace_end,
            observed_inactive_at=observed,
            next_binding_at=next_binding,
            suspension_due_at=due,
            deletion_due_at=deletion_due(exhausted_at),
        )
        await self.repository.insert_transition(
            builder_id=book.builder_id,
            episode_id=episode.id,
            transition="opened",
            effect_id=effect_id,
            occurred_at=exhausted_at,
            source=source,
            detail={
                "suspension_due_at": due.isoformat(),
                "deletion_due_at": deletion_due(exhausted_at).isoformat(),
                "grace_ends_at": grace_end.isoformat() if grace_end else None,
            },
        )
        outcome.transitions.append("opened")
        # Which episodes THIS session opened (:func:`reservation_verdict`:
        # the upload that exhausts a builder is served, the next ones are
        # not).
        self.session.info.setdefault(_OPENED_EPISODES, set()).add(episode.id)
        return episode

    async def _grace_inputs(
        self, builder_id: UUID, exhausted_at: datetime
    ) -> tuple[datetime | None, datetime | None, datetime | None, datetime | None]:
        """The binding whose grace window holds ``exhausted_at``, if any:
        ``(cycle_end_at, grace_end, observed_inactive_at, next_binding_at)``."""

        for binding in await self.repository.active_bindings(builder_id):
            cycle_end_at = start_of_civil_day(binding.cycle_end)
            window_end = add_civil_days(cycle_end_at, self.grace_days)
            if not cycle_end_at <= exhausted_at < window_end:
                continue
            is_active = await self.repository.subscription_is_active(
                binding.subscription_id
            )
            observed = None if is_active else exhausted_at
            next_binding = await self.repository.next_binding_at(
                binding.subscription_id, binding.cycle_end
            )
            grace_end = binding_grace_end(
                cycle_end_at,
                grace_days=self.grace_days,
                is_active=is_active,
                observed_inactive_at=observed,
                next_binding_at=next_binding,
            )
            return cycle_end_at, grace_end, observed, next_binding
        return None, None, None, None

    async def _observe_grace(
        self,
        episode: CreditLifecycleEpisode,
        at: datetime,
        source: LifecycleSource,
        outcome: LifecycleOutcome,
    ) -> None:
        """While the grace runs, record the first observed deactivation and
        the next binding's commit, and shorten (never extend) the grace."""

        cycle_end_at = as_utc(episode.cycle_end_at)
        grace_end = as_utc(episode.grace_ends_at)
        if cycle_end_at is None or grace_end is None or at >= grace_end:
            return
        binding = None
        for candidate in await self.repository.active_bindings(episode.builder_id):
            if start_of_civil_day(candidate.cycle_end) == cycle_end_at:
                binding = candidate
                break
        if binding is None:
            return
        changes: dict[str, datetime] = {}
        observed = as_utc(episode.observed_inactive_at)
        if observed is None and not await self.repository.subscription_is_active(
            binding.subscription_id
        ):
            observed = changes["observed_inactive_at"] = at
        next_binding = as_utc(episode.next_binding_at)
        if next_binding is None:
            found = await self.repository.next_binding_at(
                binding.subscription_id, binding.cycle_end
            )
            if found is not None:
                next_binding = changes["next_binding_at"] = found
        candidate_end = binding_grace_end(
            cycle_end_at,
            grace_days=self.grace_days,
            is_active=observed is None,
            observed_inactive_at=observed,
            next_binding_at=next_binding,
        )
        exhausted_at = as_utc(episode.exhausted_at)
        assert exhausted_at is not None
        if candidate_end < grace_end:
            changes["grace_ends_at"] = candidate_end
            due = suspension_due(
                exhausted_at,
                cycle_end=cycle_end_at,
                binding_grace_ends_at=candidate_end,
            )
            current_due = as_utc(episode.suspension_due_at)
            assert current_due is not None
            if due < current_due:
                changes["suspension_due_at"] = due
        if not changes:
            return
        await self.repository.update_episode(episode, **changes)
        if "grace_ends_at" in changes:
            await self.repository.insert_transition(
                builder_id=episode.builder_id,
                episode_id=episode.id,
                transition="grace_shortened",
                effect_id=grace_effect_id(episode.id, changes["grace_ends_at"]),
                occurred_at=at,
                source=source,
                detail={
                    "grace_ends_at": changes["grace_ends_at"].isoformat(),
                    "suspension_due_at": _iso(episode.suspension_due_at),
                },
            )
            outcome.transitions.append("grace_shortened")

    async def _reactivate(
        self,
        episode: CreditLifecycleEpisode,
        at: datetime,
        source: LifecycleSource,
        outcome: LifecycleOutcome,
    ) -> None:
        exhausted_at = as_utc(episode.exhausted_at)
        assert exhausted_at is not None
        reactivated_at = max(at, exhausted_at)
        await self.repository.update_episode(episode, reactivated_at=reactivated_at)
        await self.repository.insert_transition(
            builder_id=episode.builder_id,
            episode_id=episode.id,
            transition="reactivated",
            effect_id=transition_effect_id(episode.id, "reactivated"),
            occurred_at=reactivated_at,
            source=source,
        )
        outcome.transitions.append("reactivated")

    async def _thresholds(
        self,
        book: LedgerBook,
        at: datetime,
        closed: int,
        available: int,
        outcome: LifecycleOutcome,
    ) -> None:
        capacity = capacity_in_force_mc(book, at)
        reached = thresholds_reached(capacity, available)
        if not reached:
            return
        from src.services.credit_allocations import accounting_window

        window = accounting_window(
            (
                (lot.cycle_start, lot.cycle_end)
                for lot in book.lots
                if lot.lot_kind == "monthly"
                and lot.cycle_start is not None
                and lot.cycle_end is not None
            ),
            at,
        )
        key = arm_key(closed, window.start.isoformat())
        existing = await self.repository.threshold_alerts(book.builder_id, key)
        for threshold in reached:
            if threshold in existing:
                continue
            if await self.repository.insert_threshold_alert(
                builder_id=book.builder_id,
                arm_key=key,
                threshold=threshold,
                capacity_mc=capacity,
                available_mc=available,
                effect_id=threshold_effect_id(book.builder_id, key, threshold),
                occurred_at=at,
            ):
                outcome.thresholds.append(threshold)


# ---------------------------------------------------------------------------
# Locked evaluation (request gate and worker share it).
# ---------------------------------------------------------------------------


async def evaluate_locked(
    session: AsyncSession,
    builder_id: UUID,
    *,
    at: datetime,
    source: LifecycleSource,
) -> LifecycleOutcome:
    """Account ``FOR UPDATE`` -> swept book (start catch-up) ->
    ``settle_open_debts`` -> persist -> evaluate.  The caller commits."""

    from src.services.credit_ledger import CreditLedger

    ledger = CreditLedger(session)
    book = await ledger.open_book(builder_id, at=at)
    assert book.clock is not None
    book.settle_open_debts(
        settlement_effect_id(builder_id, book.clock),
        at=book.clock,
        effect_kind=DEBT_SETTLEMENT_EFFECT,
    )
    await ledger.persist(book, lifecycle_source=source)
    return ledger.lifecycle_outcome or _INERT


async def request_gate(
    session: AsyncSession, builder_id: UUID, *, now: datetime | None = None
) -> GateVerdict:
    """The agent gate of a metered request (its pre-check transaction).

    Cheap while nothing is due (one read of the open episode).  At or after
    the suspension deadline it evaluates under the account lock with the
    worker's effect ids (settling first, so an allowance that started pays
    and reactivates); ``suspended`` while the episode stays suspended or
    deletion-due.  The caller commits the recorded transitions."""

    instant = _aware(now or datetime.now(timezone.utc), "now")
    episode = await ContentRetentionRepository(session).open_episode(builder_id)
    if episode is None:
        return "ok"
    due = as_utc(episode.suspension_due_at)
    assert due is not None
    if instant < due:
        return "ok"
    outcome = await evaluate_locked(session, builder_id, at=instant, source="effect")
    return "suspended" if outcome.state in ("suspended", "deletion_due") else "ok"


async def tenant_suspended(
    session: AsyncSession, tenant_id: UUID, *, now: datetime | None = None
) -> bool:
    """The suspension gate of a production (API key) RAG search or media
    download -- Deliverable 3 ("suspender agente/RAG/mídia ... conforme
    deadline"): the tenant row ``FOR SHARE`` (lock order step 1), then
    :func:`request_gate` on its builder (the worker's effect ids under the
    account lock past the deadline).  ``False`` for a vanished or
    standalone tenant (their own paths answer).  The caller calls it only
    after the enforcement with the switch effective, and commits (the gate
    may record the transition).  The export is never gated."""

    from src.repositories.credit import CreditRepository

    scope = await CreditRepository(session).tenant_scope(tenant_id, lock=True)
    if scope is None or scope.builder_id is None:
        return False
    return await request_gate(session, scope.builder_id, now=now) == "suspended"


async def reservation_verdict(
    session: AsyncSession,
    *,
    builder_id: UUID,
    effect_id: str,
    charged_mc: int,
) -> CategoryVerdict:
    """Deliverable 3: new content is blocked ONLY in the exhausted category.

    Called right after the upload's reservation, in its transaction (the
    builder's account lock the reservation took is still held, so the
    decision and the reservation are one): ``category_exhausted`` only when
    the builder was already exhausted (an episode open before this upload),
    THIS reservation charged above the window's coverage (``charged_mc >
    0``) and none of that charge came from usable capacity (its debit found
    zero available: the whole delta would be new debt).  An upload whose
    content fits the category's coverage (zero delta) is accepted while the
    episode is open, and the first exhausting upload of a builder is served
    as debt (it opens the episode, like a trace).  The caller rolls back on
    a refusal: no object, reservation or episode survives it.  Traces are
    not affected, and content already stored stays usable until the
    suspension."""

    if charged_mc <= 0:
        return "ok"
    episode = await ContentRetentionRepository(session).open_episode(builder_id)
    if episode is None or episode.id in session.info.get(_OPENED_EPISODES, ()):
        return "ok"
    consumed = await session.scalar(
        select(func.coalesce(func.sum(CreditLedgerEntry.amount_mc), 0)).where(
            CreditLedgerEntry.builder_id == builder_id,
            CreditLedgerEntry.effect_id == effect_id,
            CreditLedgerEntry.entry_type == "debit",
        )
    )
    return "category_exhausted" if int(consumed or 0) == 0 else "ok"


async def builder_state(
    session: AsyncSession, builder_id: UUID
) -> tuple[LifecycleState, CreditLifecycleEpisode | None]:
    """Read-only state for APIs and gates (no lock, no write)."""

    repository = ContentRetentionRepository(session)
    episode = await repository.open_episode(builder_id)
    if episode is None:
        return "active", None
    recorded = await repository.transitions_of(episode.id)
    return (
        lifecycle_state(
            open_episode=True,
            suspended="suspended" in recorded,
            deletion_marked="deletion_due" in recorded,
        ),
        episode,
    )


# ---------------------------------------------------------------------------
# The periodic worker.
# ---------------------------------------------------------------------------


def _session_factory() -> Callable[[], AsyncSession]:
    from src.repositories.database import get_db_session_factory

    return get_db_session_factory()


async def run_lifecycle_once(
    session_factory: Callable[[], AsyncSession] | None = None,
    *,
    now: datetime | None = None,
    limit: int = WORKER_BATCH,
) -> int:
    """One tick: claims nothing with the switch off or before the
    enforcement; otherwise each candidate builder in its own transaction,
    its account claimed ``FOR UPDATE SKIP LOCKED``.  Returns the number of
    builders evaluated."""

    async def tick() -> int:
        factory = session_factory or _session_factory()
        instant = _aware(now or datetime.now(timezone.utc), "now")
        async with factory() as session:
            lifecycle = CreditLifecycle(session)
            enforcement = await lifecycle.enforcement_at()
            if enforcement is None or instant < enforcement:
                return 0
            candidates = await lifecycle.repository.lifecycle_candidates(
                now=instant,
                enforcement_at=enforcement,
                limit=limit,
                today=instant.astimezone(CREDIT_TIMEZONE).date(),
            )
            await session.rollback()
        evaluated = 0
        for builder_id in candidates:
            evaluated += await _evaluate_candidate(factory, builder_id, instant)
        return evaluated

    result = await run_credit_worker_tick(tick)
    return result or 0


async def _evaluate_candidate(
    factory: Callable[[], AsyncSession], builder_id: UUID, instant: datetime
) -> int:
    async with factory() as session:
        try:
            if not await ContentRetentionRepository(session).claim_account(builder_id):
                await session.rollback()
                return 0
            await evaluate_locked(session, builder_id, at=instant, source="worker")
            await session.commit()
            return 1
        except Exception as error:
            with suppress(Exception):
                await session.rollback()
            logger.warning(
                LIFECYCLE_LOG,
                alert="credit_lifecycle_worker",
                builder_id=str(builder_id),
                error_type=type(error).__name__,
            )
            return 0


async def run_lifecycle_worker(
    interval: float | None = None,
    *,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> None:
    """Cancellable lifespan loop (``credit_lifecycle_worker_interval_seconds``)."""

    delay = float(
        settings.credit_lifecycle_worker_interval_seconds
        if interval is None
        else interval
    )
    while True:
        try:
            await run_lifecycle_once(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                LIFECYCLE_LOG,
                alert="credit_lifecycle_worker",
                error_type=type(error).__name__,
            )
        await asyncio.sleep(delay)


def start_credit_lifecycle_worker() -> asyncio.Task[None] | None:
    """Started only with the credit config on; each tick is still guarded by
    the effective switch and the enforcement (it claims nothing before)."""

    if settings.credit_hot_path_enabled is not True:
        return None
    return asyncio.create_task(run_lifecycle_worker(), name="credit-lifecycle-worker")


async def stop_credit_lifecycle_worker(task: asyncio.Task[None] | None) -> None:
    """Cancel before the pool closes; never raises."""

    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task


__all__ = [
    "DEBT_GRACE",
    "RETENTION_DAYS",
    "THRESHOLDS",
    "CreditLifecycle",
    "LifecycleOutcome",
    "LifecycleState",
    "add_civil_days",
    "arm_key",
    "binding_grace_end",
    "builder_state",
    "capacity_in_force_mc",
    "deletion_due",
    "evaluate_locked",
    "lifecycle_state",
    "request_gate",
    "reservation_verdict",
    "run_lifecycle_once",
    "run_lifecycle_worker",
    "start_credit_lifecycle_worker",
    "stop_credit_lifecycle_worker",
    "suspension_due",
    "tenant_suspended",
    "thresholds_reached",
    "transition_effect_id",
]
