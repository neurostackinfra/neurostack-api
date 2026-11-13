"""The credit workers per builder on real PostgreSQL (cobranca-credito-por-builder G3).

With the global ``credit_enforcement_at`` unset, a per-builder mark
(``credit_enforcement_marks``) makes the enforced path apply to ONE builder.
The three workers -- the lifecycle worker (``run_lifecycle_once``), the
enforced-reply publisher (``CreditResponsePublisher``) and the orphan pass
(``run_credit_orphan_reconciliation_once``) -- run when the global is set or
when any mark event was ever recorded, and the lifecycle worker only ever
selects marked builders, each bounded by its own instant.  Proven here, each
proof on a fresh clone of one migrated (047) database:

* Success Criterion 1: an unmarked builder with an expired lot (and
  observational rows, one of a deleted clinic), another one unmarked after
  its episode opened (now due, with an allowance starting), another one
  unmarked inside its episode's binding grace, and a builder deleted while
  marked get NO write from any worker while another builder is marked -- no
  candidate source selects them; a builder marked after its lot expired is
  not swept either (its own instant, not another builder's); a builder
  unmarked, or unmarked and re-marked at a later instant, between the
  selection and its lock is skipped with nothing written; a mark dated after
  the tick's instant is not yet in force;
* Success Criterion 4: a marked builder with only a courtesy lot whose lot
  expires with no traffic gets the exhaustion episode, its 72 h clock and the
  suspension; a new courtesy reactivates it; an allowance that starts
  reactivates it with no traffic;
* Success Criterion 12: after the last ``unmark`` a pending enforced reply is
  still published and an orphan still reconciled;
* with the global unset and no mark event ever, each worker runs ONE read
  more than before (``_ANY_MARK_READ``; none with the 047 gate closed) and
  writes nothing; with the global set no worker reads the marks.
  debito-cortesia-automatico (DESIGN Decision 20): with no courtesy lot
  either, the publisher and the orphan pass read one statement more
  (``ANY_COURTESY_LOT_READ``) and stop; a courtesy lot alone turns them on
  (tests/integration/test_credit_effective_enforcement_postgres.py).

How an unmarked builder is shown untouched: every row of every ``credit_*``
table that has a ``builder_id`` (and its tenant's outbox, claims and
observations) is read before and after and must be identical -- no insert,
update or delete -- and no statement any worker sent carries its id as a
parameter, so the worker never selected, locked, read or swept it.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text

from src.repositories.content_retention import ContentRetentionRepository
from src.services import (
    credit_courtesy,
    credit_lifecycle,
    credit_metering,
    credit_rollout,
)
from src.services.credit_ledger import CreditLedger, start_of_civil_day
from src.services.credit_lifecycle import DEBT_GRACE, add_civil_days, builder_state
from src.services.credit_rollout import (
    OrphanSweepState,
    run_credit_orphan_reconciliation_once,
)
from tests.integration import test_credit_metering_postgres as metering_suite
from tests.integration import test_credit_orphan_reconciliation_postgres as orphans
from tests.integration.credit_rollout_world import (
    ANY_COURTESY_LOT_READ,
    civil_today,
    clean,
    execute,
    force_credit_switch,
    forget_enforcement_latches,
    make_builder,
    make_cycle,
    make_monthly_grant,
    make_subscription,
    make_tenant,
    mark_events,
    observation,
    record_mark,
    scalar,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

UTC = timezone.utc
# Every instant of these proofs is in the past of the real clock (the mark
# writer and the outbox use the database clock); the ledger's reference
# clock follows ``_Clock`` (as in test_credit_retention_postgres.py).
_BASE = (datetime.now(UTC) - timedelta(days=90)).replace(
    minute=0, second=0, microsecond=0
)
_HOUR = timedelta(hours=1)
# The one statement of ``CreditRepository.enforcement_at()`` (the global).
_ENFORCEMENT_READ = re.compile(
    r"SELECT credit_enforcement_state\.credit_enforcement_at\s+"
    r"FROM credit_enforcement_state\s+"
    r"WHERE credit_enforcement_state\.id = \$1::SMALLINT"
)
# The one statement of ``CreditRepository.any_enforcement_mark()``: the
# workers' one extra read with the global unset.
_ANY_MARK_READ = re.compile(
    r"SELECT credit_enforcement_marks\.id\s+"
    r"FROM credit_enforcement_marks\s+LIMIT \$1::INTEGER"
)
_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE)\b|\bFOR UPDATE\b", re.IGNORECASE)


@dataclass
class _Clock:
    now: datetime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    """The ledger's reference clock (the database's, otherwise): the worker
    runs at instants weeks apart, inside the clock tolerance."""

    current = _Clock(_BASE)

    async def reference_now(_self: CreditLedger) -> datetime:
        return current.now

    monkeypatch.setattr(CreditLedger, "reference_now", reference_now)
    yield current


@pytest.fixture(autouse=True)
def _switch(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The switch effective on a reading at this image's head (the 047 gate
    open), and a fresh process for the D1 latches."""

    force_credit_switch(monkeypatch)
    forget_enforcement_latches()
    yield
    forget_enforcement_latches()


# ---------------------------------------------------------------------------
# Seeds and probes.
# ---------------------------------------------------------------------------


async def _courtesy(
    harness: _Harness, clock: _Clock, builder_id: UUID, at: datetime, credits: int = 10
) -> datetime:
    """A courtesy grant at ``at`` (the super admin's service); returns the
    lot's ``expires_at``."""

    clock.now = at
    async with harness.sessions() as session, session.begin():
        granted = await credit_courtesy.grant(
            session,
            builder_id=builder_id,
            credits=credits,
            reason="per-builder workers test",
            actor_user_id=uuid4(),
            idempotency_key=f"workers-{uuid4()}",
            at=at,
        )
    return granted.expires_at


async def _tick(harness: _Harness, clock: _Clock, at: datetime) -> int:
    clock.now = at
    return await credit_lifecycle.run_lifecycle_once(harness.sessions, now=at)


async def _builder_tables(harness: _Harness) -> list[str]:
    """The ``credit_*`` tables with a ``builder_id`` -- but the marks: the
    operator's own table (a proof here writes the mark events it needs)."""

    async with harness.engine.connect() as connection:
        return [
            str(name)
            for (name,) in await connection.execute(
                text(
                    "SELECT table_name FROM information_schema.columns WHERE "
                    "table_schema = 'public' AND column_name = 'builder_id' AND "
                    "table_name LIKE 'credit\\_%' AND table_name <> "
                    "'credit_enforcement_marks' ORDER BY table_name"
                )
            )
        ]


async def _snapshot(
    harness: _Harness, builder_id: UUID, tenant_id: UUID | None = None
) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of the builder in every ``credit_*`` table with a
    ``builder_id`` (plus its tenant's outbox), whole rows, in id order."""

    tables = await _builder_tables(harness)
    assert {"credit_ledger_entries", "credit_lots", "credit_lifecycle_episodes"} <= set(
        tables
    )
    rows: dict[str, list[tuple[Any, ...]]] = {}
    async with harness.engine.connect() as connection:
        for table in tables:
            rows[table] = [
                tuple(row)
                for row in await connection.execute(
                    text(f"SELECT * FROM {table} WHERE builder_id = :b ORDER BY 1"),
                    {"b": builder_id},
                )
            ]
        if tenant_id is not None:
            rows["credit_response_outbox"] = [
                tuple(row)
                for row in await connection.execute(
                    text(
                        "SELECT * FROM credit_response_outbox WHERE tenant_id = :t "
                        "ORDER BY 1"
                    ),
                    {"t": tenant_id},
                )
            ]
    return rows


async def _ledger(harness: _Harness, builder_id: UUID) -> list[tuple[str, int]]:
    async with harness.engine.connect() as connection:
        return [
            (str(kind), int(amount))
            for kind, amount in await connection.execute(
                text(
                    "SELECT entry_type, amount_mc FROM credit_ledger_entries "
                    "WHERE builder_id = :b ORDER BY occurred_at, id"
                ),
                {"b": builder_id},
            )
        ]


@dataclass(frozen=True)
class _Episode:
    exhausted_at: datetime
    suspension_due_at: datetime
    reactivated_at: datetime | None
    cycle_end_at: datetime | None
    grace_ends_at: datetime | None


async def _episodes(harness: _Harness, builder_id: UUID) -> list[_Episode]:
    async with harness.engine.connect() as connection:
        return [
            _Episode(*row)
            for row in await connection.execute(
                text(
                    "SELECT exhausted_at, suspension_due_at, reactivated_at, "
                    "cycle_end_at, grace_ends_at FROM credit_lifecycle_episodes "
                    "WHERE builder_id = :b ORDER BY exhausted_at"
                ),
                {"b": builder_id},
            )
        ]


async def _transitions(
    harness: _Harness, builder_id: UUID
) -> list[tuple[str, datetime, str]]:
    async with harness.engine.connect() as connection:
        return [
            (str(name), at, str(source))
            for name, at, source in await connection.execute(
                text(
                    "SELECT transition, occurred_at, source FROM "
                    "credit_lifecycle_transitions WHERE builder_id = :b ORDER BY "
                    "occurred_at, created_at"
                ),
                {"b": builder_id},
            )
        ]


async def _state(harness: _Harness, builder_id: UUID) -> str:
    async with harness.sessions() as session:
        state, _episode = await builder_state(session, builder_id)
        await session.rollback()
    return state


@dataclass
class _Sent:
    statements: list[tuple[str, Any]] = field(default_factory=list)

    @property
    def sql(self) -> list[str]:
        return [statement.strip() for statement, _ in self.statements]

    def mention(self, value: UUID) -> list[str]:
        """The statements that carried ``value`` (as a parameter or inline)."""

        wanted = str(value)
        return [
            statement
            for statement, parameters in self.statements
            if wanted in statement or wanted in repr(parameters)
        ]

    def writes(self) -> list[str]:
        return [s for s in self.sql if _WRITE.search(s)]


@contextmanager
def _sent(harness: _Harness) -> Iterator[_Sent]:
    """Every statement (with its parameters) the harness engine sends."""

    seen = _Sent()
    engine = harness.engine.sync_engine

    def record(
        _conn: Any, _cursor: Any, statement: str, parameters: Any, *_rest: Any
    ) -> None:
        seen.statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def _observational_claim(
    harness: _Harness, tenant_id: UUID, builder_id: UUID
) -> None:
    """An OBSERVATIONAL ingress claim (every builder has them before any
    enforcement) with no effect: no orphan anchor (those are enforced)."""

    await execute(
        harness,
        """
        INSERT INTO credit_ingress_claims (
            id, tenant_id, builder_id, source, principal_id, idempotency_key,
            provider_message_id, interaction_id, payload_sha256, decision_version,
            mode
        ) VALUES (:id, :tenant, :builder, 'events', :principal, :key, NULL,
                  :interaction, :sha, 1, 'observational')
        """,
        id=uuid4(),
        tenant=tenant_id,
        builder=builder_id,
        principal=f"events:{tenant_id}",
        key=f"observed-{uuid4()}",
        interaction=uuid4(),
        sha="d" * 64,
    )


async def _set_global(harness: _Harness, at: datetime) -> None:
    await execute(
        harness,
        "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, set_by, "
        "set_at) VALUES (1, :at, 'test', :at)",
        at=at,
    )


def _publisher(harness: _Harness, sender: Any) -> Any:
    return credit_metering.CreditResponsePublisher(
        sender, session_factory=harness.sessions
    )


# ---------------------------------------------------------------------------
# Success Criterion 1: no worker write to a builder that is not marked.
# ---------------------------------------------------------------------------


async def test_no_worker_touches_an_unmarked_builder_while_another_is_marked(
    clean: _Harness, clock: _Clock
) -> None:
    """``marked`` is marked.  The others are not, and each holds what one
    candidate source of the lifecycle worker selects (with the global):

    * ``unmarked`` -- never marked; its courtesy lot expired like the marked
      one's (the expired-lot source), plus observational rows: a claim
      without effect and an effect of a deleted clinic (no orphan anchor, no
      deletion closure);
    * ``released`` -- marked, its episode opened, unmarked; its 72 h passed
      (the due source) and a monthly allowance starts today (the
      starting-allowance source);
    * ``graced`` -- marked, its monthly allowance expired at its cycle end,
      its episode opened inside the binding grace, unmarked; the grace still
      runs (the grace source).

    One tick of each worker: the marked builder gets its episode; the others
    get nothing -- no row inserted, changed or deleted, and no statement
    carries their ids."""

    marked, unmarked, released, graced = [await make_builder(clean) for _ in range(4)]
    expired = await _courtesy(clean, clock, marked, _BASE)
    assert await _courtesy(clean, clock, unmarked, _BASE) == expired
    at = expired + _HOUR

    unmarked_tenant = await make_tenant(clean, unmarked)
    await observation(clean, unmarked_tenant, unmarked, _BASE)  # as in production
    await _observational_claim(clean, unmarked_tenant, unmarked)
    gone = await make_tenant(clean, unmarked)
    await observation(clean, gone, unmarked, _BASE)
    await execute(clean, "DELETE FROM tenants WHERE id = :t", t=gone)

    # ``released``: marked, its lot expires five days before the others',
    # the worker opens its episode, then it is unmarked; an allowance starts
    # on the civil day of the tick.
    early = await _courtesy(clean, clock, released, _BASE - timedelta(days=5))
    await record_mark(clean, released, occurred_at=_BASE - timedelta(days=5) + _HOUR)
    assert await _tick(clean, clock, early + _HOUR) == 1
    [episode] = await _episodes(clean, released)
    assert episode.exhausted_at == early
    await record_mark(clean, released, "unmark", occurred_at=early + 2 * _HOUR)
    today = civil_today(at)
    await make_monthly_grant(
        clean,
        builder_id=released,
        subscription_id=await make_subscription(clean, released),
        cycle_id=uuid4(),
        start=today,
        end=today + timedelta(days=30),
        with_binding=False,
    )
    # Its 72 h passed: a worker selecting it would record its suspension.
    assert at > episode.suspension_due_at

    # ``graced``: its bound monthly allowance ends the day before the tick
    # and expires with no traffic, inside the binding grace.
    cycle_end = civil_today(expired) - timedelta(days=1)
    subscription = await make_subscription(clean, graced)
    await make_monthly_grant(
        clean,
        builder_id=graced,
        subscription_id=subscription,
        cycle_id=await make_cycle(
            clean,
            graced,
            subscription,
            start=cycle_end - timedelta(days=30),
            end=cycle_end,
        ),
        start=cycle_end - timedelta(days=30),
        end=cycle_end,
    )
    await record_mark(clean, graced, occurred_at=_BASE - _HOUR)
    cycle_end_at = start_of_civil_day(cycle_end)
    assert await _tick(clean, clock, cycle_end_at + _HOUR) == 1
    [grace] = await _episodes(clean, graced)
    assert grace.exhausted_at == grace.cycle_end_at == cycle_end_at
    assert grace.grace_ends_at == add_civil_days(cycle_end_at, 5)
    await record_mark(clean, graced, "unmark", occurred_at=cycle_end_at + 2 * _HOUR)
    # Still inside the grace, nothing else due: only the grace source.
    assert grace.grace_ends_at > at and grace.suspension_due_at > at

    await record_mark(clean, marked, occurred_at=_BASE + _HOUR)
    others = {unmarked: unmarked_tenant, released: None, graced: None}
    before = {
        builder_id: await _snapshot(clean, builder_id, tenant)
        for builder_id, tenant in others.items()
    }

    sender = metering_suite._Recorded()
    with _sent(clean) as sent:
        assert await _tick(clean, clock, at) == 1
        tick = await _publisher(clean, sender).run_once()
        assert await run_credit_orphan_reconciliation_once(clean.sessions) == 0

    # The marked builder: its lot's expiry, then the episode at the deadline.
    assert [e.exhausted_at for e in await _episodes(clean, marked)] == [expired]
    assert await _ledger(clean, marked) == [("grant", 10_000), ("expiry", 10_000)]
    assert tick is not None and tick.enforced and tick.outcomes == []
    # The others: not one row moved, and not one statement named them.
    for builder_id, tenant in others.items():
        assert await _snapshot(clean, builder_id, tenant) == before[builder_id]
        assert sent.mention(builder_id) == []
    assert ("expiry", 10_000) not in await _ledger(clean, unmarked)
    assert await _transitions(clean, released) == [
        ("opened", early, "worker")
    ]  # never suspended
    assert [t[0] for t in await _transitions(clean, graced)] == ["opened"]
    assert await _state(clean, unmarked) == "active"


async def test_a_builder_marked_after_its_lot_expired_is_bounded_by_its_own_instant(
    clean: _Harness, clock: _Clock
) -> None:
    """Two marked builders, one tick: the one marked before the expiry gets
    the episode; the one marked AFTER its lot expired is not swept -- that
    expiry is before its instant, whatever the other builder's instant."""

    early, late = await make_builder(clean), await make_builder(clean)
    expired = await _courtesy(clean, clock, early, _BASE)
    assert await _courtesy(clean, clock, late, _BASE) == expired
    await record_mark(clean, early, occurred_at=_BASE + _HOUR)
    await record_mark(clean, late, occurred_at=expired + timedelta(minutes=30))
    before = await _snapshot(clean, late)

    with _sent(clean) as sent:
        assert await _tick(clean, clock, expired + _HOUR) == 1

    assert [e.exhausted_at for e in await _episodes(clean, early)] == [expired]
    assert await _snapshot(clean, late) == before
    # Selected by its own bound only: never claimed, locked or swept.
    assert [s for s in sent.mention(late) if _WRITE.search(s)] == []
    assert not [s for s in sent.mention(late) if "credit_accounts" in s]


async def test_a_builder_deleted_while_marked_is_never_a_candidate(
    clean: _Harness, clock: _Clock
) -> None:
    """Its marks outlive it (no foreign key, migration 047) and its credit
    history too; the worker skips it like a builder that is not marked."""

    alive, deleted = await make_builder(clean), await make_builder(clean)
    expired = await _courtesy(clean, clock, alive, _BASE)
    await _courtesy(clean, clock, deleted, _BASE)
    for builder_id in (alive, deleted):
        await record_mark(clean, builder_id, occurred_at=_BASE + _HOUR)
    await execute(clean, "DELETE FROM builders WHERE id = :b", b=deleted)
    before = await _snapshot(clean, deleted)

    assert await _tick(clean, clock, expired + _HOUR) == 1

    assert [e.exhausted_at for e in await _episodes(clean, alive)] == [expired]
    assert await _snapshot(clean, deleted) == before


async def test_the_candidates_take_exactly_one_scope(clean: _Harness) -> None:
    """The global's instant or the marked builders, never both nor neither;
    no marked builder reads nothing."""

    now = datetime.now(UTC)
    async with clean.sessions() as session:
        repository = ContentRetentionRepository(session)
        with pytest.raises(ValueError, match="exactly one"):
            await repository.lifecycle_candidates(now=now, limit=5)
        with pytest.raises(ValueError, match="exactly one"):
            await repository.lifecycle_candidates(
                now=now, enforcement_at=now, marked={uuid4(): now}, limit=5
            )
        with _sent(clean) as sent:
            assert (
                await repository.lifecycle_candidates(now=now, marked={}, limit=5) == []
            )
        await session.rollback()
    assert sent.sql == []


async def test_a_builder_unmarked_after_the_selection_is_skipped_under_its_lock(
    clean: _Harness, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``unmark`` commits between the tick's selection and the claim of
    the builder's account: the effective enforcement re-read under the lock
    is no longer the mark it was selected with, so nothing is written (the
    account row it claimed is released by the rollback, after that one
    read: the request path's re-check under the same lock)."""

    builder_id = await make_builder(clean)
    expired = await _courtesy(clean, clock, builder_id, _BASE)
    await record_mark(clean, builder_id, occurred_at=_BASE + _HOUR)
    before = await _snapshot(clean, builder_id)
    original = ContentRetentionRepository.claim_account
    claimed: list[UUID] = []

    async def unmarked_first(
        self: ContentRetentionRepository, claimed_id: UUID
    ) -> bool:
        claimed.append(claimed_id)
        await record_mark(clean, claimed_id, "unmark")
        return await original(self, claimed_id)

    monkeypatch.setattr(ContentRetentionRepository, "claim_account", unmarked_first)

    assert await _tick(clean, clock, expired + _HOUR) == 0

    assert claimed == [builder_id]  # selected while marked
    assert await _snapshot(clean, builder_id) == before


async def test_a_builder_re_marked_after_the_selection_is_skipped_under_its_lock(
    clean: _Harness, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``unmark`` and a new ``mark`` (a LATER instant) commit between the
    selection and the claim: still marked, but not by the mark it was
    selected with -- the re-check compares the instant and the origin, so
    nothing is written (the next tick decides on the new instant)."""

    builder_id = await make_builder(clean)
    expired = await _courtesy(clean, clock, builder_id, _BASE)
    await record_mark(clean, builder_id, occurred_at=_BASE + _HOUR)
    before = await _snapshot(clean, builder_id)
    original = ContentRetentionRepository.claim_account
    claimed: list[UUID] = []

    async def re_marked_first(
        self: ContentRetentionRepository, claimed_id: UUID
    ) -> bool:
        claimed.append(claimed_id)
        await record_mark(clean, claimed_id, "unmark")
        await record_mark(clean, claimed_id, "mark")
        return await original(self, claimed_id)

    monkeypatch.setattr(ContentRetentionRepository, "claim_account", re_marked_first)

    assert await _tick(clean, clock, expired + _HOUR) == 0

    assert claimed == [builder_id]  # selected under the first mark
    assert [kind for kind, _at in await mark_events(clean, builder_id)] == [
        "mark",
        "unmark",
        "mark",
    ]
    assert await _snapshot(clean, builder_id) == before


async def test_a_mark_dated_after_the_tick_is_not_yet_in_force(
    clean: _Harness, clock: _Clock
) -> None:
    """A marked builder whose episode is due, whose latest mark is dated
    after the tick's instant (a worker clock behind the database clock): not
    in force yet, so the tick neither claims nor evaluates it.  Two ``mark``
    rows in a row are planted by SQL -- the operator command, which refuses
    an ``unmark`` with an open episode, cannot produce this; it pins the
    clock-skew defense of ``run_lifecycle_once`` (marks at or before the
    tick only)."""

    builder_id = await make_builder(clean)
    await record_mark(clean, builder_id, occurred_at=_BASE - _HOUR)
    expired = await _courtesy(clean, clock, builder_id, _BASE)
    assert await _tick(clean, clock, expired + _HOUR) == 1
    [episode] = await _episodes(clean, builder_id)
    await record_mark(clean, builder_id)  # the database clock: after the tick
    due = episode.suspension_due_at + timedelta(minutes=1)
    before = await _snapshot(clean, builder_id)

    with _sent(clean) as sent:
        assert await _tick(clean, clock, due) == 0

    assert not [s for s in sent.mention(builder_id) if "FOR UPDATE" in s]
    assert await _snapshot(clean, builder_id) == before
    assert [t[0] for t in await _transitions(clean, builder_id)] == ["opened"]


# ---------------------------------------------------------------------------
# Success Criterion 4: the marked builder's episode, suspension, reactivation.
# ---------------------------------------------------------------------------


async def test_a_marked_courtesy_lot_that_expires_without_traffic_opens_the_episode(
    clean: _Harness, clock: _Clock
) -> None:
    """Only a courtesy lot, no traffic: its expiry opens the episode at the
    deadline, 72 h later the worker records the suspension, and a new
    courtesy reactivates the account."""

    builder_id = await make_builder(clean)
    await record_mark(clean, builder_id, occurred_at=_BASE - _HOUR)
    expired = await _courtesy(clean, clock, builder_id, _BASE)
    # Before the deadline nothing is due.
    assert await _tick(clean, clock, expired - _HOUR) == 0
    assert await _episodes(clean, builder_id) == []

    assert await _tick(clean, clock, expired + _HOUR) == 1
    [episode] = await _episodes(clean, builder_id)
    assert episode.exhausted_at == expired
    assert episode.suspension_due_at == expired + DEBT_GRACE
    assert episode.reactivated_at is None
    assert await _ledger(clean, builder_id) == [("grant", 10_000), ("expiry", 10_000)]
    assert await _transitions(clean, builder_id) == [("opened", expired, "worker")]
    assert await _state(clean, builder_id) == "grace"

    due = expired + DEBT_GRACE
    assert await _tick(clean, clock, due - timedelta(minutes=1)) == 0
    assert await _tick(clean, clock, due + timedelta(minutes=1)) == 1
    assert await _transitions(clean, builder_id) == [
        ("opened", expired, "worker"),
        ("suspended", due, "worker"),
    ]
    assert await _state(clean, builder_id) == "suspended"

    reopened = due + 8 * _HOUR
    await _courtesy(clean, clock, builder_id, reopened)
    [closed] = await _episodes(clean, builder_id)
    assert closed.reactivated_at == reopened
    assert [t[0] for t in await _transitions(clean, builder_id)] == [
        "opened",
        "suspended",
        "reactivated",
    ]
    assert await _state(clean, builder_id) == "active"


async def test_an_allowance_starting_reactivates_a_marked_builder_without_traffic(
    clean: _Harness, clock: _Clock
) -> None:
    """An episode open, then an early-paid monthly allowance: at its start
    the worker (the starting-allowance candidates) reactivates the marked
    builder with no traffic."""

    builder_id = await make_builder(clean)
    await record_mark(clean, builder_id, occurred_at=_BASE - _HOUR)
    expired = await _courtesy(clean, clock, builder_id, _BASE)
    assert await _tick(clean, clock, expired + _HOUR) == 1
    assert len(await _episodes(clean, builder_id)) == 1
    start = civil_today(expired) + timedelta(days=1)
    await make_monthly_grant(
        clean,
        builder_id=builder_id,
        subscription_id=await make_subscription(clean, builder_id),
        cycle_id=uuid4(),
        start=start,
        end=start + timedelta(days=30),
        with_binding=False,
    )
    starts = start_of_civil_day(start)
    assert starts + _HOUR < expired + DEBT_GRACE  # nothing else is due

    assert await _tick(clean, clock, starts + _HOUR) == 1

    [episode] = await _episodes(clean, builder_id)
    assert episode.reactivated_at == starts + _HOUR
    assert [t[0] for t in await _transitions(clean, builder_id)] == [
        "opened",
        "reactivated",
    ]


# ---------------------------------------------------------------------------
# Success Criterion 12: the outbox and the orphans after the last unmark.
# ---------------------------------------------------------------------------


async def test_after_the_last_unmark_a_pending_enforced_reply_is_still_published(
    clean: _Harness,
) -> None:
    seeded = await metering_suite._tenant(clean.engine)
    assert seeded.builder_id is not None
    await record_mark(clean, seeded.builder_id)
    row = await metering_suite._outbox_row(clean.engine, seeded)  # a crash
    await record_mark(clean, seeded.builder_id, "unmark")
    sender = metering_suite._Recorded()

    tick = await _publisher(clean, sender).run_once()

    assert tick is not None and tick.enforced and tick.outcomes == ["delivered"]
    assert sender.sent == [str(row)]
    state, payload, attempts = await metering_suite._outbox_state(clean.engine, row)
    assert (state, attempts) == ("delivered", 1) and "phone" not in payload


async def test_after_the_last_unmark_an_orphan_is_still_reconciled(
    clean: _Harness,
) -> None:
    """The marked builder's enforced decision without its effect stays open
    (the gauge rises), and its deleted clinic's enforced effect gets its
    deletion closure."""

    builder_id = await make_builder(clean)
    tenant_id = await make_tenant(clean, builder_id)
    await record_mark(clean, builder_id)
    await orphans._claim(clean, tenant_id, builder_id)  # its effect never written
    gone = await make_tenant(clean, builder_id)
    closed = f"trace:{gone}:{uuid4()}:decision:1"
    await orphans._effect(clean, gone, builder_id, closed, "trace_no_response")
    await execute(clean, "DELETE FROM tenants WHERE id = :t", t=gone)
    await record_mark(clean, builder_id, "unmark")
    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)  # a sentinel no pass writes

    published = await run_credit_orphan_reconciliation_once(
        clean.sessions, state=OrphanSweepState()
    )

    assert published == 1 and orphans._orphan_gauge() == 1
    assert await orphans._closures(clean, builder_id) == 1


# ---------------------------------------------------------------------------
# What the workers read: one statement more with the global unset, the same
# statements with it set.
# ---------------------------------------------------------------------------


async def _dormant_world(
    harness: _Harness, clock: _Clock, *, courtesy: bool = True
) -> tuple[UUID, datetime]:
    """A builder whose courtesy lot expired, a stale outbox row and an
    enforced claim without its effect: everything a worker would act on if
    it ran.  Returns the builder and an instant one hour after its expiry.

    ``courtesy=False`` (debito-cortesia-automatico, DESIGN Decision 20): no
    courtesy lot at all -- one would turn the publisher and the orphan pass
    on by itself -- and the instant is the same."""

    builder_id = await make_builder(harness)
    if courtesy:
        expired = await _courtesy(harness, clock, builder_id, _BASE)
    else:
        expired = _BASE + timedelta(days=30)
    seeded = await metering_suite._tenant(harness.engine)
    await metering_suite._outbox_row(harness.engine, seeded)
    assert seeded.builder_id is not None
    await orphans._claim(harness, seeded.tenant_id, seeded.builder_id)
    return builder_id, expired + _HOUR


async def _all_credit_rows(harness: _Harness) -> dict[str, list[tuple[Any, ...]]]:
    async with harness.engine.connect() as connection:
        tables = [
            str(name)
            for (name,) in await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "AND tablename LIKE 'credit\\_%' ORDER BY tablename"
                )
            )
        ]
        return {
            table: [
                tuple(row)
                for row in await connection.execute(
                    text(f'SELECT * FROM "{table}" ORDER BY 1')
                )
            ]
            for table in tables
        }


@pytest.mark.parametrize("gate_open", [True, False], ids=["047", "gate-closed"])
async def test_with_nothing_ever_marked_each_worker_reads_once_more_and_writes_nothing(
    clean: _Harness, clock: _Clock, monkeypatch: pytest.MonkeyPatch, gate_open: bool
) -> None:
    """The global unset and no mark event: each worker reads the global (its
    one statement before the per-builder change) and then -- with the 047
    gate open -- whether any mark event exists, and stops.  With the gate
    closed (a reading below 047) it reads the global only, as before.

    debito-cortesia-automatico (DESIGN Decision 20): with no courtesy lot
    either, the publisher and the orphan pass read one statement more --
    whether any courtesy lot exists (the 046 gate is open on both readings)
    -- and stop; the lifecycle worker never reads it."""

    if not gate_open:
        force_credit_switch(monkeypatch, revision="046")
    _builder, at = await _dormant_world(clean, clock, courtesy=False)
    before = await _all_credit_rows(clean)
    expected = [_ENFORCEMENT_READ, _ANY_MARK_READ] if gate_open else [_ENFORCEMENT_READ]
    rule_trigger = [*expected, ANY_COURTESY_LOT_READ]
    sender = metering_suite._Recorded()
    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)

    ticks: dict[str, list[str]] = {}
    for name, run in (
        ("lifecycle", lambda: _tick(clean, clock, at)),
        ("publisher", lambda: _publisher(clean, sender).run_once()),
        ("orphans", lambda: run_credit_orphan_reconciliation_once(clean.sessions)),
    ):
        with _sent(clean) as sent:
            result = await run()
        ticks[name] = sent.sql
        if name == "publisher":
            assert result is not None and not result.enforced and not result.outcomes
        else:
            assert result == 0

    for name, statements in ticks.items():
        reads = expected if name == "lifecycle" else rule_trigger
        assert len(statements) == len(reads), (name, statements)
        assert all(
            pattern.fullmatch(statement)
            for pattern, statement in zip(reads, statements, strict=True)
        ), (name, statements)
    assert sender.sent == []
    assert orphans._orphan_gauge() == -1  # dormant: not even the gauge
    assert await _all_credit_rows(clean) == before


async def test_with_the_global_set_no_worker_reads_the_marks(
    clean: _Harness, clock: _Clock
) -> None:
    """The global set and a mark recorded: the workers run as before for
    EVERY builder (the unmarked one is evaluated too) and never read the
    marks or the builders."""

    builder_id, at = await _dormant_world(clean, clock)
    other = await make_builder(clean)
    await record_mark(clean, other, occurred_at=_BASE - _HOUR)
    await _set_global(clean, _BASE - timedelta(days=1))
    sender = metering_suite._Recorded()

    with _sent(clean) as sent:
        assert await _tick(clean, clock, at) == 1
        tick = await _publisher(clean, sender).run_once()
        # The claim without its effect and the outbox row naming no effect.
        assert await run_credit_orphan_reconciliation_once(clean.sessions) == 2

    assert tick is not None and tick.enforced and tick.outcomes == ["delivered"]
    assert len(await _episodes(clean, builder_id)) == 1
    assert not [s for s in sent.sql if "credit_enforcement_marks" in s]
    assert not [s for s in sent.sql if re.search(r"\bFROM builders\b", s)]
    assert sum(1 for s in sent.sql if _ENFORCEMENT_READ.fullmatch(s)) >= 3
    assert await scalar(clean, "SELECT count(*) FROM credit_enforcement_marks") == 1
