"""``python -m src.ops.credit_charge_mark mark|unmark`` on real PostgreSQL
(cobranca-credito-por-builder Group 4).

Every proof runs the module's own entry point (``amain``: argument parsing,
the switch refreshed in this process at the database's ``alembic_version``,
one read-write transaction, the one JSON document) against a fresh database
copied from one migrated template (head, 047 or later):

* ``mark -> unmark -> mark`` writes one event each, with the author and the
  trimmed reason, ``occurred_at`` strictly increasing (also past a
  future-dated latest event), and the request path's reader
  (``CreditRepository.effective_enforcement``) returns the mark, then none,
  then the second mark at its own, later instant (the baseline moves); the
  rows written are append-only (UPDATE and DELETE refused by the trigger);
* the transaction takes, in order, the author ``FOR SHARE``, the builder
  ``FOR KEY SHARE`` and the account ``FOR UPDATE`` and writes nothing but the
  event (and the builder's empty account when it had none);
* the refusals, each writing nothing: an author who is missing, inactive or
  not a ``super_admin``; a builder that does not exist, was deleted with its
  marks left, or is deleted by a transaction the command waits for; the
  global L10 armed (both commands); already marked / not marked; an unusable
  coverage (a quarantined cycle, a current cycle without a binding); a
  linked tenant's channel outside ``zapi``/``meta_cloud``/none (only
  ``whatsapp_provider`` is read, no credential is printed); a window already
  opened by an earlier mark; an open episode or open debt for ``unmark`` --
  including a debt committed by a transaction holding the account lock the
  ``unmark`` waits on;
* two concurrent ``mark`` of one builder write one event.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

import src.models  # noqa: F401  (registers every mapper)
from src.config.settings import settings
from src.ops import credit_charge_mark as ops
from src.repositories.credit import CreditRepository
from src.services.credit_allocations import accounting_window
from src.services.credit_rollout import CreditRolloutService
from src.services.credit_switch import stop_credit_switch
from tests.integration.credit_rollout_world import (
    PLANT,
    civil_today,
    clean,
    execute,
    make_builder,
    make_cycle,
    make_subscription,
    make_tenant,
    mark_events,
    plant,
    record_mark,
    scalar,
    subscribed_builder,
    template,
)
from tests.integration.test_billing_fact_worker_postgres import _Harness

pytestmark = pytest.mark.integration

__all__ = ["clean", "template"]

REASON = "piloto de cobrança por builder"
SECRETS = (
    "zapi-token-NEVER-PRINT-71c9",
    "uazapi-token-NEVER-PRINT-55aa",
    "meta-permanent-token-NEVER-PRINT-0b1e",
    "sk-llm-key-NEVER-PRINT-9f3d",
    "provider-value-that-is-a-secret-NEVER-PRINT",
)
CREDENTIAL_KEYS = ("zapi_token", "uazapi_token", "meta_permanent_token", "llm_api_key")


# ---------------------------------------------------------------------------
# Harness: the module's real entry point on a fresh database.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    code: int
    document: dict[str, Any]
    printed: str


@pytest.fixture
async def world(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Harness]:
    """A fresh database with the credit configuration on (the module still
    refreshes the switch itself); the process verdict is reset afterwards."""

    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    try:
        yield clean
    finally:
        await stop_credit_switch(None)


def _argv(command: str, builder: UUID, author: UUID, reason: str = REASON) -> list[str]:
    return [
        command,
        "--builder-id",
        str(builder),
        "--author-user-id",
        str(author),
        "--reason",
        reason,
    ]


async def run(
    harness: _Harness,
    command: str,
    builder: UUID,
    author: UUID,
    *,
    reason: str = REASON,
    sessions: Any = None,
) -> Outcome:
    out, err = io.StringIO(), io.StringIO()
    code = await ops.amain(
        _argv(command, builder, author, reason),
        session_factory=sessions or harness.sessions,
        stdout=out,
        stderr=err,
    )
    (line,) = out.getvalue().splitlines()
    document = json.loads(line)
    (log,) = [
        record
        for record in (
            json.loads(raw) for raw in err.getvalue().splitlines() if raw[:1] == "{"
        )
        if record.get("event") == ops.LOG_EVENT
    ]
    assert document["exit_code"] == code
    assert {key: log[key] for key in document} == document
    return Outcome(code, document, out.getvalue() + err.getvalue())


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


async def make_user(
    harness: _Harness, *, role: str = "super_admin", active: bool = True
) -> UUID:
    user_id = uuid4()
    home = await make_tenant(harness, None)
    await execute(
        harness,
        "INSERT INTO users (id, tenant_id, email, password_hash, role, is_active) "
        "VALUES (:id, :tenant, :email, 'x', CAST(:role AS userrole), :active)",
        id=user_id,
        tenant=home,
        email=f"{user_id.hex}@ops.invalid",
        role=role,
        active=active,
    )
    return user_id


async def bare_builder(harness: _Harness) -> UUID:
    """A builder without a credit account (``make_builder`` creates one)."""

    builder_id = uuid4()
    await execute(
        harness,
        "INSERT INTO builders (id, name) VALUES (:id, 'Mark builder')",
        id=builder_id,
    )
    return builder_id


async def _count(harness: _Harness, table: str, builder: UUID) -> int:
    return int(
        await scalar(
            harness, f"SELECT count(*) FROM {table} WHERE builder_id = :b", b=builder
        )
    )


async def _effective(harness: _Harness, builder: UUID) -> Any:
    async with harness.sessions() as session:
        found = await CreditRepository(session).effective_enforcement(
            builder, marks_readable=True
        )
        await session.rollback()
    return found


def _refused(outcome: Outcome, command: str, *reasons: str) -> None:
    assert outcome.code == ops.EXIT_CODES["refused"] == 20, outcome.document
    assert outcome.document["command"] == command
    assert outcome.document["verdict"] == "refused"
    assert outcome.document["reasons"] == list(reasons)
    assert outcome.document["change"]["event"] is None


def _locked_table(statement: str) -> str:
    found = re.search(r"\bFROM (\w+)", statement)
    assert found is not None, statement
    return found.group(1)


def _writes(statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if not statement.lstrip().upper().startswith(("SELECT", "WITH", "SET"))
    ]


# ---------------------------------------------------------------------------
# The happy sequence.
# ---------------------------------------------------------------------------


async def test_mark_unmark_mark_and_the_request_path_reader(world: _Harness) -> None:
    author = await make_user(world)
    builder = await bare_builder(world)
    assert (await _effective(world, builder)).origin is None

    with _statements(world.engine) as seen:
        marked = await run(world, "mark", builder, author, reason=f"  {REASON}  ")
    assert marked.code == 0, marked.document
    assert marked.document["verdict"] == "marked"
    written = marked.document["change"]["event"]
    assert (written["kind"], written["actor_user_id"], written["reason"]) == (
        "mark",
        str(author),
        REASON,
    )
    first = datetime.fromisoformat(written["occurred_at"])
    # The locks, in order, then the only writes: the empty account (this
    # builder had none) and the one event.
    locks = [
        (index, statement)
        for index, statement in enumerate(seen)
        if re.search(r"FOR (SHARE|KEY SHARE|UPDATE)\s*$", statement)
    ]
    assert [
        (_locked_table(statement), statement.rsplit("FOR ", 1)[1].strip())
        for _, statement in locks
    ] == [
        ("users", "SHARE"),
        ("builders", "KEY SHARE"),
        ("credit_accounts", "UPDATE"),
    ]
    writes = _writes(seen)
    assert len(writes) == 2, writes
    assert writes[0].startswith("INSERT INTO credit_accounts")
    assert "ON CONFLICT" in writes[0]
    assert writes[1].startswith("INSERT INTO credit_enforcement_marks")
    assert locks[-1][0] < seen.index(writes[1])
    # The coverage of the ``mark`` refusal is read under the account lock.
    coverage_reads = [
        index
        for index, statement in enumerate(seen)
        if re.search(
            r"\b(billing_subscription_cycles|credit_enforcement_binding)\b", statement
        )
    ]
    assert coverage_reads, "the mark read no coverage"
    assert min(coverage_reads) > locks[-1][0], (coverage_reads, locks[-1][0])
    effective = await _effective(world, builder)
    assert (effective.at, effective.origin) == (first, "mark")
    assert marked.document["enforcement"] == {
        "global_at": None,
        "effective_at": first.isoformat(),
        "origin": "mark",
    }

    with _statements(world.engine) as seen:
        unmarked = await run(world, "unmark", builder, author, reason="fim do piloto")
    assert unmarked.code == 0, unmarked.document
    assert unmarked.document["verdict"] == "unmarked"
    assert [w.split(" (")[0] for w in _writes(seen)] == [
        "INSERT INTO credit_accounts",
        "INSERT INTO credit_enforcement_marks",
    ]
    stopped = datetime.fromisoformat(
        unmarked.document["change"]["event"]["occurred_at"]
    )
    assert (await _effective(world, builder)).origin is None
    assert unmarked.document["enforcement"]["origin"] == "none"

    again = await run(world, "mark", builder, author, reason="de novo")
    assert again.code == 0, again.document
    second = datetime.fromisoformat(again.document["change"]["event"]["occurred_at"])
    effective = await _effective(world, builder)
    # The second mark moves the baseline: it starts at its own instant.
    assert (effective.at, effective.origin) == (second, "mark")
    assert first < stopped < second

    assert await mark_events(world, builder) == [
        ("mark", first),
        ("unmark", stopped),
        ("mark", second),
    ]
    async with world.engine.connect() as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT actor_user_id, reason FROM credit_enforcement_marks "
                    "WHERE builder_id = :b ORDER BY occurred_at"
                ),
                {"b": builder},
            )
        ).all()
    assert [(UUID(str(a)), r) for a, r in stored] == [
        (author, REASON),
        (author, "fim do piloto"),
        (author, "de novo"),
    ]
    assert await _count(world, "credit_accounts", builder) == 1

    # The rows the command wrote are append-only.
    for statement in (
        "UPDATE credit_enforcement_marks SET reason = 'x' WHERE builder_id = :b",
        "DELETE FROM credit_enforcement_marks WHERE builder_id = :b",
    ):
        with pytest.raises(DBAPIError):
            await execute(world, statement, b=builder)
    assert len(await mark_events(world, builder)) == 3


async def test_occurred_at_stays_past_a_later_latest_event(world: _Harness) -> None:
    """The database clock behind the builder's latest event (a future-dated
    row): the command's event is one microsecond after it, never a tie."""

    author = await make_user(world)
    builder = await make_builder(world)
    ahead = datetime.now(timezone.utc) + timedelta(hours=2)
    await record_mark(world, builder, "unmark", occurred_at=ahead)
    marked = await run(world, "mark", builder, author)
    assert marked.code == 0, marked.document
    at = datetime.fromisoformat(marked.document["change"]["event"]["occurred_at"])
    assert at == ahead + timedelta(microseconds=1)
    assert [kind for kind, _ in await mark_events(world, builder)] == ["unmark", "mark"]


async def test_a_long_reason_round_trips(world: _Harness) -> None:
    author = await make_user(world)
    builder = await make_builder(world)
    reason = "ç" * ops.REASON_MAX_LENGTH
    marked = await run(world, "mark", builder, author, reason=reason)
    assert marked.code == 0, marked.document
    assert (
        await scalar(
            world,
            "SELECT reason FROM credit_enforcement_marks WHERE builder_id = :b",
            b=builder,
        )
        == reason
    )
    too_long = await run(world, "mark", builder, author, reason=reason + "ç")
    assert (too_long.code, too_long.document["reasons"]) == (30, ["usage"])


# ---------------------------------------------------------------------------
# Author and builder.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "active", "exists"),
    [
        ("super_admin", False, True),
        ("clinic_admin", True, True),
        ("builder", True, True),
        ("super_admin", True, False),
    ],
)
async def test_the_author_must_be_an_active_super_admin(
    world: _Harness, role: str, active: bool, exists: bool
) -> None:
    author = await make_user(world, role=role, active=active) if exists else uuid4()
    builder = await bare_builder(world)
    await record_mark(world, builder)  # so that ``unmark`` is otherwise valid
    for command in ("mark", "unmark"):
        with _statements(world.engine) as seen:
            outcome = await run(world, command, builder, author)
        _refused(outcome, command, "author_not_active_super_admin")
        assert _writes(seen) == []
    assert len(await mark_events(world, builder)) == 1
    assert await _count(world, "credit_accounts", builder) == 0


async def test_an_unknown_or_deleted_builder_is_refused(world: _Harness) -> None:
    author = await make_user(world)
    for command in ("mark", "unmark"):
        _refused(
            await run(world, command, uuid4(), author), command, "builder_not_found"
        )
    # A builder deleted with its marks left (no foreign key): its last event
    # is a ``mark``, yet neither command writes for it.
    builder = await bare_builder(world)
    assert (await run(world, "mark", builder, author)).code == 0
    # The account is append-only and has no foreign key either: it stays.
    await execute(world, "DELETE FROM builders WHERE id = :b", b=builder)
    for command in ("unmark", "mark"):
        _refused(
            await run(world, command, builder, author), command, "builder_not_found"
        )
    assert [kind for kind, _ in await mark_events(world, builder)] == ["mark"]
    assert await _count(world, "credit_accounts", builder) == 1


async def test_a_builder_deleted_concurrently_is_waited_for(world: _Harness) -> None:
    """``FOR KEY SHARE`` waits for a deleting transaction and then sees the
    row gone: no mark is ever written for a deleted builder."""

    author = await make_user(world)
    builder = await bare_builder(world)
    engine, sessions = world.factory()
    try:
        async with world.engine.connect() as deleting:
            transaction = await deleting.begin()
            await deleting.execute(
                text("DELETE FROM builders WHERE id = :b"), {"b": builder}
            )
            pending = asyncio.create_task(
                run(world, "mark", builder, author, sessions=sessions)
            )
            done, _ = await asyncio.wait({pending}, timeout=1.5)
            assert not done, "the mark did not wait for the deleting transaction"
            await transaction.commit()
        outcome = await asyncio.wait_for(pending, timeout=30)
    finally:
        await engine.dispose()
    _refused(outcome, "mark", "builder_not_found")
    assert await mark_events(world, builder) == []


# ---------------------------------------------------------------------------
# The global L10 and the latest event.
# ---------------------------------------------------------------------------


async def test_the_armed_global_refuses_both_commands(world: _Harness) -> None:
    author = await make_user(world)
    marked_builder = await make_builder(world)
    other = await bare_builder(world)
    assert (await run(world, "mark", marked_builder, author)).code == 0
    armed = datetime.now(timezone.utc) - timedelta(days=1)
    await execute(
        world,
        "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, set_by, "
        "set_at) VALUES (1, :at, 'test', :at)",
        at=armed,
    )
    unmarked = await run(world, "unmark", marked_builder, author)
    _refused(unmarked, "unmark", "global_enforcement_armed")
    assert unmarked.document["enforcement"] == {
        "global_at": armed.isoformat(),
        "effective_at": armed.isoformat(),
        "origin": "global",
    }
    _refused(
        await run(world, "mark", other, author), "mark", "global_enforcement_armed"
    )
    assert [kind for kind, _ in await mark_events(world, marked_builder)] == ["mark"]
    assert await mark_events(world, other) == []
    # The refusal rolled back the account its lock step created.
    assert await _count(world, "credit_accounts", other) == 0


async def test_already_marked_and_not_marked_are_refused(world: _Harness) -> None:
    author = await make_user(world)
    builder = await make_builder(world)
    _refused(await run(world, "unmark", builder, author), "unmark", "not_marked")
    assert (await run(world, "mark", builder, author)).code == 0
    _refused(await run(world, "mark", builder, author), "mark", "already_marked")
    assert (await run(world, "unmark", builder, author)).code == 0
    _refused(await run(world, "unmark", builder, author), "unmark", "not_marked")
    assert [kind for kind, _ in await mark_events(world, builder)] == ["mark", "unmark"]


async def test_two_concurrent_marks_write_one_event(world: _Harness) -> None:
    author = await make_user(world)
    builder = await bare_builder(world)
    factories = [world.factory(), world.factory()]
    try:
        outcomes = await asyncio.gather(
            *(
                run(world, "mark", builder, author, sessions=sessions)
                for _, sessions in factories
            )
        )
    finally:
        for engine, _ in factories:
            await engine.dispose()
    assert sorted(outcome.document["verdict"] for outcome in outcomes) == [
        "marked",
        "refused",
    ]
    (loser,) = [o for o in outcomes if o.document["verdict"] == "refused"]
    assert loser.document["reasons"] == ["already_marked"]
    assert [kind for kind, _ in await mark_events(world, builder)] == ["mark"]
    assert await _count(world, "credit_accounts", builder) == 1


# ---------------------------------------------------------------------------
# mark refusals: coverage, channel, window.
# ---------------------------------------------------------------------------


async def _coverage(harness: _Harness, builder: UUID) -> Any:
    async with harness.sessions() as session:
        found = await CreditRolloutService(session).binding_coverage(
            builder, at=datetime.now(timezone.utc)
        )
        await session.rollback()
    return found


async def test_mark_refuses_a_quarantined_cycle(world: _Harness) -> None:
    author = await make_user(world)
    builder = await make_builder(world)
    subscription = await make_subscription(world, builder)
    today = civil_today()
    await make_cycle(
        world,
        builder,
        subscription,
        start=today - timedelta(days=40),
        end=today - timedelta(days=10),
        state="quarantined",
    )
    coverage = await _coverage(world, builder)
    assert coverage.quarantined is True and coverage.usable is False
    outcome = await run(world, "mark", builder, author)
    _refused(outcome, "mark", "coverage_unusable")
    assert outcome.document["coverage"]["usable"] is False
    assert await mark_events(world, builder) == []


async def test_mark_refuses_a_reconciliation_failure(world: _Harness) -> None:
    """A current cycle of an active subscription with no binding:
    ``credit_pending`` for every enforced request."""

    author = await make_user(world)
    builder = await make_builder(world)
    subscription = await make_subscription(world, builder)
    today = civil_today()
    await make_cycle(
        world,
        builder,
        subscription,
        start=today - timedelta(days=10),
        end=today + timedelta(days=20),
    )
    coverage = await _coverage(world, builder)
    assert coverage.reconciliation_failure is True and coverage.usable is False
    outcome = await run(world, "mark", builder, author)
    _refused(outcome, "mark", "coverage_unusable")
    assert outcome.document["coverage"] == {
        "usable": False,
        "reconciliation_failure": True,
    }
    assert await mark_events(world, builder) == []


async def test_a_bound_subscription_is_usable_and_marks(world: _Harness) -> None:
    author = await make_user(world)
    subscribed = await subscribed_builder(world)
    assert (await _coverage(world, subscribed.builder_id)).usable is True
    outcome = await run(world, "mark", subscribed.builder_id, author)
    assert outcome.code == 0, outcome.document
    assert outcome.document["coverage"] == {
        "usable": True,
        "reconciliation_failure": False,
    }


async def _tenant_with(
    harness: _Harness, builder: UUID, credentials: dict[str, Any] | None
) -> UUID:
    tenant = await make_tenant(harness, builder)
    if credentials is not None:
        await execute(
            harness,
            "INSERT INTO clinic_configs (id, tenant_id, api_credentials) "
            "VALUES (:id, :tenant, CAST(:credentials AS JSONB))",
            id=uuid4(),
            tenant=tenant,
            credentials=json.dumps(credentials),
        )
    return tenant


@pytest.mark.parametrize(
    ("credentials", "allowed"),
    [
        (None, True),
        ({"llm_provider": "openai", "llm_api_key": SECRETS[3]}, True),
        ({"whatsapp_provider": None}, True),
        ({"whatsapp_provider": "zapi", "zapi_token": SECRETS[0]}, True),
        ({"whatsapp_provider": "meta_cloud", "meta_permanent_token": SECRETS[2]}, True),
        ({"whatsapp_provider": "uazapi", "uazapi_token": SECRETS[1]}, False),
        ({"whatsapp_provider": "evolution"}, False),
        ({"whatsapp_provider": "dialog360"}, False),
        ({"whatsapp_provider": "twilio"}, False),
        ({"whatsapp_provider": ""}, False),
        ({"whatsapp_provider": 7}, False),
        ({"whatsapp_provider": SECRETS[4], "llm_api_key": SECRETS[3]}, False),
    ],
    ids=lambda value: repr(value)[:40],
)
async def test_mark_refuses_a_channel_outside_the_allowlist(
    world: _Harness, credentials: dict[str, Any] | None, allowed: bool
) -> None:
    author = await make_user(world)
    builder = await bare_builder(world)
    await _tenant_with(world, builder, {"whatsapp_provider": "zapi"})
    tenant = await _tenant_with(world, builder, credentials)
    with _statements(world.engine) as seen:
        outcome = await run(world, "mark", builder, author)
    if allowed:
        assert outcome.code == 0, outcome.document
        assert [kind for kind, _ in await mark_events(world, builder)] == ["mark"]
        assert await _count(world, "credit_accounts", builder) == 1
    else:
        _refused(outcome, "mark", "channel_not_allowed")
        assert await mark_events(world, builder) == []
        assert await _count(world, "credit_accounts", builder) == 0
    listed = {
        row["tenant_id"]: row["whatsapp_provider"]
        for row in outcome.document["tenants"]
    }
    raw = None if credentials is None else credentials.get("whatsapp_provider")
    assert listed[str(tenant)] == ops.provider_label(None if raw is None else str(raw))
    for secret in (*SECRETS, *CREDENTIAL_KEYS, "api_credentials"):
        assert secret not in outcome.printed, secret
    for statement in seen:
        for found in re.finditer(r"api_credentials", statement):
            assert statement.startswith(
                "api_credentials ->> 'whatsapp_provider' AS whatsapp_provider",
                found.start(),
            ), statement


async def test_mark_refuses_a_window_opened_by_an_earlier_mark(
    world: _Harness,
) -> None:
    author = await make_user(world)
    builder = await make_builder(world)
    assert (await run(world, "mark", builder, author)).code == 0
    # The request path opened the current window under that mark.
    window = accounting_window((), datetime.now(timezone.utc))
    await execute(
        world,
        "INSERT INTO credit_allocations (id, builder_id, account_id, window_start, "
        "window_end, category, allocation_kind, source_key, units, target_mc, "
        "charged_mc) SELECT :id, :b, id, :start, :end, 'rag', 'baseline', :key, 0, "
        "0, 0 FROM credit_accounts WHERE builder_id = :b",
        id=uuid4(),
        b=builder,
        start=window.start,
        end=window.end,
        key=f"opening:test:{uuid4()}",
    )
    assert (await run(world, "unmark", builder, author)).code == 0
    outcome = await run(world, "mark", builder, author)
    _refused(outcome, "mark", "window_already_opened")
    assert outcome.document["change"]["window"]["window_opened"] is True
    assert outcome.document["change"]["window"]["window_start"] == (
        window.start.isoformat()
    )
    assert [kind for kind, _ in await mark_events(world, builder)] == ["mark", "unmark"]

    # An opening of an earlier window does not refuse.
    other = await make_builder(world)
    previous = accounting_window((), datetime.now(timezone.utc) - timedelta(days=45))
    assert previous.start < window.start
    await execute(
        world,
        "INSERT INTO credit_allocations (id, builder_id, account_id, window_start, "
        "window_end, category, allocation_kind, source_key, units, target_mc, "
        "charged_mc) SELECT :id, :b, id, :start, :end, 'rag', 'baseline', :key, 0, "
        "0, 0 FROM credit_accounts WHERE builder_id = :b",
        id=uuid4(),
        b=other,
        start=previous.start,
        end=previous.end,
        key=f"opening:test:{uuid4()}",
    )
    assert (await run(world, "mark", other, author)).code == 0


# ---------------------------------------------------------------------------
# unmark refusals: open episode, open debt (under the account lock).
# ---------------------------------------------------------------------------

_EPISODE = (
    "INSERT INTO credit_lifecycle_episodes (id, builder_id, account_id, "
    "opened_effect_id, exhausted_at, suspension_due_at, deletion_due_at) "
    "SELECT :id, :b, id, :effect, :at, :due, :deletion FROM credit_accounts "
    "WHERE builder_id = :b"
)
_DEBT = (
    "INSERT INTO credit_effects (id, builder_id, effect_id, effect_kind) "
    "VALUES (:effect_row, :b, :effect, 'debit')",
    "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
    "lot_id, entry_type, amount_mc, occurred_at) SELECT :entry, :b, id, :effect, "
    "NULL, 'debt', 5000, :at FROM credit_accounts WHERE builder_id = :b",
    "INSERT INTO credit_debts (id, builder_id, account_id, debt_source_kind, "
    "debt_source_effect_id, original_grant_lot_id, amount_mc, created_at) "
    "SELECT :debt, :b, id, 'debit', :effect, NULL, 5000, :at FROM credit_accounts "
    "WHERE builder_id = :b",
)


def _lifecycle_params(builder: UUID) -> dict[str, Any]:
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=5)
    return {
        "id": uuid4(),
        "b": builder,
        "effect": f"debit:test:{uuid4()}",
        "effect_row": uuid4(),
        "entry": uuid4(),
        "debt": uuid4(),
        "at": at,
        "due": at + timedelta(hours=72),
        "deletion": at + timedelta(days=30),
    }


@pytest.mark.parametrize(
    ("episode", "debt", "reasons"),
    [
        (True, False, ("open_episode",)),
        (False, True, ("open_debt",)),
        (True, True, ("open_episode", "open_debt")),
    ],
)
async def test_unmark_refuses_an_open_episode_or_debt(
    world: _Harness, episode: bool, debt: bool, reasons: tuple[str, ...]
) -> None:
    author = await make_user(world)
    builder = await make_builder(world)
    assert (await run(world, "mark", builder, author)).code == 0
    params = _lifecycle_params(builder)
    if debt:
        for statement in _DEBT:
            await plant(world, statement, **params)
    if episode:
        await plant(world, _EPISODE, **params)
    outcome = await run(world, "unmark", builder, author)
    _refused(outcome, "unmark", *reasons)
    lifecycle = outcome.document["lifecycle"]
    assert lifecycle["open_episode"] == (str(params["id"]) if episode else None)
    assert (lifecycle["open_debt_mc"], lifecycle["open_debts"]) == (
        (5_000, 1) if debt else (0, 0)
    )
    assert [kind for kind, _ in await mark_events(world, builder)] == ["mark"]
    assert (await _effective(world, builder)).origin == "mark"


async def test_unmark_waits_on_the_account_lock_and_sees_its_debt(
    world: _Harness,
) -> None:
    """A transaction holding the builder's account ``FOR UPDATE`` (an
    enforced request) commits a debt while ``unmark`` waits on that lock:
    the ``unmark`` reads after it and refuses."""

    author = await make_user(world)
    builder = await make_builder(world)
    assert (await run(world, "mark", builder, author)).code == 0
    params = _lifecycle_params(builder)
    engine, sessions = world.factory()
    try:
        async with world.engine.connect() as holder:
            transaction = await holder.begin()
            await holder.execute(text(PLANT))
            await holder.execute(
                text("SELECT id FROM credit_accounts WHERE builder_id = :b FOR UPDATE"),
                {"b": builder},
            )
            pending = asyncio.create_task(
                run(world, "unmark", builder, author, sessions=sessions)
            )
            done, _ = await asyncio.wait({pending}, timeout=1.5)
            assert not done, "the unmark did not wait for the account lock"
            for statement in _DEBT:
                await holder.execute(text(statement), params)
            await transaction.commit()
        outcome = await asyncio.wait_for(pending, timeout=30)
    finally:
        await engine.dispose()
    _refused(outcome, "unmark", "open_debt")
    assert [kind for kind, _ in await mark_events(world, builder)] == ["mark"]


async def test_unmark_of_a_clean_account_is_accepted(world: _Harness) -> None:
    """The account had a 5-credit debt, fully settled by its monthly lot
    (the ``debt`` entry, the debt row, the ``debt_settlement`` entry and
    the settlement row), and an episode, reactivated: ``unmark`` reads the
    OPEN ones only and is accepted."""

    author = await make_user(world)
    subscribed = await subscribed_builder(world)
    builder = subscribed.builder_id
    assert (await run(world, "mark", builder, author)).code == 0
    params = _lifecycle_params(builder)
    for statement in _DEBT:
        await plant(world, statement, **params)
    await plant(
        world,
        "INSERT INTO credit_ledger_entries (id, builder_id, account_id, effect_id, "
        "lot_id, entry_type, amount_mc, occurred_at) SELECT :entry, :b, id, "
        ":effect, :lot, 'debt_settlement', 5000, :at FROM credit_accounts "
        "WHERE builder_id = :b",
        entry=uuid4(),
        b=builder,
        effect=subscribed.grant.effect_id,
        lot=subscribed.grant.lot_id,
        at=params["at"] + timedelta(hours=1),
    )
    await plant(
        world,
        "INSERT INTO credit_debt_settlements (id, builder_id, debt_id, "
        "settling_grant_lot_id, amount_mc, effect_id) VALUES (:id, :b, :debt, "
        ":lot, 5000, :effect)",
        id=uuid4(),
        b=builder,
        debt=params["debt"],
        lot=subscribed.grant.lot_id,
        effect=subscribed.grant.effect_id,
    )
    await plant(world, _EPISODE, **params)
    await plant(
        world,
        "UPDATE credit_lifecycle_episodes SET reactivated_at = :at WHERE id = :id",
        id=params["id"],
        at=datetime.now(timezone.utc),
    )
    assert await _count(world, "credit_debts", builder) == 1
    assert await _count(world, "credit_debt_settlements", builder) == 1
    outcome = await run(world, "unmark", builder, author)
    assert outcome.code == 0, outcome.document
    assert outcome.document["lifecycle"] == {
        "open_episode": None,
        "open_debt_mc": 0,
        "open_debts": 0,
    }
    assert (await _effective(world, builder)).origin is None
