"""Operator requeue of ``billing.fact.v1`` deliveries that exhausted retries.

A one-off ECS task, started from the service's own task definition, runs::

    python -m src.ops.billing_fact_requeue plan
    python -m src.ops.billing_fact_requeue requeue

so the database credentials never leave the task.  There is no HTTP route.
The procedure around it is in ``docs/runbooks/asaas-billing-reconciliation.md``.

Subcommands (default ``plan``):

``plan``
    Read-only (a ``READ ONLY`` transaction on PostgreSQL): counts what
    ``requeue`` would reopen and what needs an investigation instead.
``requeue``
    Reopens at most ``--limit`` eligible deliveries (default 50, 1-500),
    oldest first, in one transaction, then reports the same counts.

Eligible, and nothing else.  A delivery is reopened only when ALL hold:

* ``state = 'blocked'``;
* NO row in ``billing_fact_acks`` names its fact, whatever its outcome or
  digests.  ``BillingFactPublisher._due_claim`` never claims a delivery with
  an ack, so reopening one blocked by a divergent or ``rejected`` ack would
  never present it again; those are counted as ``blocked_with_ack`` and are
  an investigation (runbook), never a requeue;
* the publisher's own evidence of retry exhaustion exists: a ``backlog``
  alert whose detail is ``RETRY_EXHAUSTED_DETAIL``.  A ``blocked`` delivery
  without an ack and without that evidence was not blocked by this code
  path (manual SQL, an unknown writer) and is counted as
  ``blocked_without_evidence``: an investigation, never a requeue.

Reopening sets ``state = 'pending'``, ``attempt_count = 0`` and
``last_attempt_at = NULL``: the row looks like a freshly produced delivery,
so the worker presents it again on its next tick with the full
``BILLING_FACT_WORKER_MAX_ATTEMPTS`` budget.  The fact, its handoff and every
earlier alert are untouched (they are append-only); the event id is the
same, so the consumer's inbox de-duplicates a fact it already holds.

Audit.  Each reopened delivery gets exactly one new, append-only row in
``billing_fact_reconciliation_alerts``: ``kind = 'backlog'``, detail
``REQUEUE_DETAIL`` (stored as its SHA-256), ``observed_at`` = the run's
clock.  It is inserted in the transaction that reopens the row and only for
rows this run actually moved, so a replay, a second run or a concurrent run
adds nothing.  The alert table has no actor column: who ran the task is
known only from the ECS task's own trail (CloudTrail ``RunTask``, the task's
log stream).  That gap is recorded in the runbook as an owner decision.

Idempotent.  Only ``blocked`` rows are selected and a reopened row is
``pending``, so a second run reopens nothing and writes nothing.  The
candidates are locked ``FOR UPDATE SKIP LOCKED`` (a row another session is
holding is left for the next run and counted in ``eligible_remaining``), and
the ``UPDATE`` re-checks every condition in a fresh statement, so an ack
committed after the candidate read still keeps its delivery ``blocked``.

Output.  Exactly one JSON line on stdout and one JSON log line on stderr,
built only from closed vocabularies and counts: never an id, a payload or
the database URL.  The exit code is 0 only for ``clean`` (``EXIT_CODES``):
``pending`` (10) means eligible deliveries remain (``plan`` found some, or
``requeue`` hit ``--limit`` or a locked row: run it again), ``investigate``
(20) means a ``blocked`` delivery that this command must not reopen exists.

This module is part of the ASAAS producer set: it never imports credit or
ledger code (``tests/models/test_credit_models.py``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Final, NoReturn, TextIO
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import ColumnElement, and_, func, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Every ``src`` import builds the global settings, which raise on an invalid
# environment.  The failure is kept so the command still prints its one JSON
# line instead of a traceback (the ``src.ops.asaas_webhook`` pattern).
try:
    from src.config.settings import Settings
    from src.models.billing import (
        BillingFactAck,
        BillingFactDelivery,
        BillingFactReconciliationAlert,
    )
    from src.services.billing_outbox import RETRY_EXHAUSTED_DETAIL
except Exception as exc:  # reported by ``main`` as one JSON line
    _IMPORT_ERROR: BaseException | None = exc
else:
    _IMPORT_ERROR = None

LOGGER_NAME: Final = "src.ops.billing_fact_requeue"
SCHEMA: Final = "neurostack.billing_fact_requeue.v1"
LOG_EVENT: Final = "billing_fact_requeue"
COMMANDS: Final = ("plan", "requeue")
EXIT_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "clean": 0,
        "pending": 10,
        "investigate": 20,
        "unconfigured": 30,
        "error": 40,
    }
)
ACTIONS: Final = frozenset({"none", "requeued"})
COUNT_NAMES: Final = (
    "eligible_before",
    "requeued",
    "eligible_remaining",
    "blocked_with_ack",
    "blocked_without_evidence",
)
REASONS: Final = frozenset(
    {
        "usage",
        "settings_invalid",
        "import_failed",
        "database_error",
        "unexpected",
        "eligible_remaining",
        "blocked_with_ack",
        "blocked_without_evidence",
    }
)
DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 500
# The fixed, sanitized audit detail of a requeue (stored only as SHA-256).
# It reuses the existing ``backlog`` alert kind: no schema change.
REQUEUE_DETAIL: Final = "operator requeue"
# Bounded waits on PostgreSQL: the candidates are SKIP LOCKED, so only the
# alert insert can wait (on the outbox row's key share lock).
_LOCK_TIMEOUT: Final = "5s"
_STATEMENT_TIMEOUT: Final = "60s"
_SETTINGS_FIELD_REASON: Final = re.compile(r"^settings_invalid:[a-z][a-z0-9_]{0,63}$")
_SETTINGS_FIELD: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def detail_digest(detail: str) -> str:
    """SHA-256 hex of a fixed alert detail, as ``billing_outbox`` stores it."""

    return hashlib.sha256(detail.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(COUNT_NAMES, 0)


@dataclass(frozen=True, slots=True)
class RunResult:
    """The one document a run prints; validated against closed vocabularies."""

    command: str | None
    verdict: str
    action: str = "none"
    reasons: tuple[str, ...] = ()
    counts: Mapping[str, int] = field(default_factory=_zero_counts)
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.verdict not in EXIT_CODES:
            raise ValueError("verdict outside the closed vocabulary")
        if self.action not in ACTIONS:
            raise ValueError("action outside the closed vocabulary")
        if self.command is not None and self.command not in COMMANDS:
            raise ValueError("command outside the closed vocabulary")
        for reason in self.reasons:
            if reason not in REASONS and not _SETTINGS_FIELD_REASON.fullmatch(reason):
                raise ValueError("reason outside the closed vocabulary")
        if sorted(self.counts) != sorted(COUNT_NAMES) or not all(
            type(value) is int and value >= 0 for value in self.counts.values()
        ):
            raise ValueError("counts outside the closed schema")
        if self.limit is not None and not 1 <= self.limit <= MAX_LIMIT:
            raise ValueError("limit outside its bounds")
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))
        object.__setattr__(self, "counts", dict(self.counts))

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "command": self.command,
            "verdict": self.verdict,
            "action": self.action,
            "reasons": list(self.reasons),
            "counts": dict(self.counts),
            "limit": self.limit,
        }


def verdict_for(counts: Mapping[str, int]) -> tuple[str, tuple[str, ...]]:
    """``investigate`` outranks ``pending``; ``clean`` only when both are zero."""

    reasons = tuple(
        name
        for name in ("blocked_with_ack", "blocked_without_evidence")
        if counts[name]
    )
    if reasons:
        if counts["eligible_remaining"]:
            reasons = (*reasons, "eligible_remaining")
        return "investigate", reasons
    if counts["eligible_remaining"]:
        return "pending", ("eligible_remaining",)
    return "clean", ()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _any_ack() -> ColumnElement[bool]:
    """``EXISTS`` any ack for the delivery's fact, exactly as ``_due_claim``.

    Outcome and digests are deliberately ignored: any ack row keeps the
    publisher from claiming the delivery, so any ack forbids a requeue.
    """

    return (
        select(BillingFactAck.id)
        .where(BillingFactAck.outbox_event_id == BillingFactDelivery.outbox_event_id)
        .correlate(BillingFactDelivery)
        .exists()
    )


def _exhaustion_evidence() -> ColumnElement[bool]:
    """``EXISTS`` the publisher's ``retry cap exhausted`` alert for the fact."""

    return (
        select(BillingFactReconciliationAlert.id)
        .where(
            BillingFactReconciliationAlert.outbox_event_id
            == BillingFactDelivery.outbox_event_id,
            BillingFactReconciliationAlert.kind == "backlog",
            BillingFactReconciliationAlert.detail_sha256
            == detail_digest(RETRY_EXHAUSTED_DETAIL),
        )
        .correlate(BillingFactDelivery)
        .exists()
    )


def _blocked() -> ColumnElement[bool]:
    return BillingFactDelivery.state == "blocked"


def requeue_eligible() -> ColumnElement[bool]:
    """Blocked by retry exhaustion, with evidence, and without any ack."""

    return and_(_blocked(), ~_any_ack(), _exhaustion_evidence())


async def _count(session: AsyncSession, condition: ColumnElement[bool]) -> int:
    value = await session.scalar(
        select(func.count()).select_from(BillingFactDelivery).where(condition)
    )
    return int(value or 0)


async def _bound_waits(session: AsyncSession, *, read_only: bool) -> None:
    if session.bind.dialect.name != "postgresql":
        return
    if read_only:
        await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    await session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))


async def _requeue(session: AsyncSession, *, limit: int, observed_at: datetime) -> int:
    """Reopen at most ``limit`` eligible deliveries; one audit row per reopen."""

    candidates = list(
        await session.scalars(
            select(BillingFactDelivery.id)
            .where(requeue_eligible())
            .order_by(BillingFactDelivery.created_at, BillingFactDelivery.id)
            .limit(limit)
            .with_for_update(of=BillingFactDelivery, skip_locked=True)
        )
    )
    if not candidates:
        return 0
    # A fresh statement re-checks every condition on the locked rows: an ack
    # committed after the candidate read keeps its delivery ``blocked``.
    reopened: list[UUID] = list(
        await session.scalars(
            update(BillingFactDelivery)
            .where(BillingFactDelivery.id.in_(candidates), requeue_eligible())
            .values(
                state="pending",
                attempt_count=0,
                last_attempt_at=None,
                updated_at=func.now(),
            )
            .returning(BillingFactDelivery.outbox_event_id)
            .execution_options(synchronize_session=False)
        )
    )
    digest = detail_digest(REQUEUE_DETAIL)
    session.add_all(
        BillingFactReconciliationAlert(
            id=uuid4(),
            outbox_event_id=outbox_event_id,
            kind="backlog",
            detail_sha256=digest,
            observed_at=observed_at,
        )
        for outbox_event_id in reopened
    )
    await session.flush()
    return len(reopened)


async def run(
    command: str,
    sessions: Callable[[], AsyncSession],
    *,
    limit: int = DEFAULT_LIMIT,
    now: Callable[[], datetime] | None = None,
) -> RunResult:
    """Run one subcommand in one transaction; the caller maps raises."""

    if command not in COMMANDS:
        raise ValueError("command outside the closed vocabulary")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError("limit outside its bounds")
    clock = now or (lambda: datetime.now(timezone.utc))
    counts = _zero_counts()
    async with sessions() as session, session.begin():
        await _bound_waits(session, read_only=command == "plan")
        counts["eligible_before"] = await _count(session, requeue_eligible())
        if command == "requeue" and counts["eligible_before"]:
            observed_at = clock()
            if observed_at.tzinfo is None:
                raise ValueError("the requeue clock must be timezone-aware")
            counts["requeued"] = await _requeue(
                session, limit=limit, observed_at=observed_at
            )
        counts["eligible_remaining"] = await _count(session, requeue_eligible())
        counts["blocked_with_ack"] = await _count(session, and_(_blocked(), _any_ack()))
        counts["blocked_without_evidence"] = await _count(
            session, and_(_blocked(), ~_any_ack(), ~_exhaustion_evidence())
        )
    verdict, reasons = verdict_for(counts)
    return RunResult(
        command=command,
        verdict=verdict,
        action="requeued" if counts["requeued"] else "none",
        reasons=reasons,
        counts=counts,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError()


def _limit(value: str) -> int:
    if not (value.isascii() and value.isdecimal()):
        raise argparse.ArgumentTypeError("limit")
    number = int(value)
    if not 1 <= number <= MAX_LIMIT:
        raise argparse.ArgumentTypeError("limit")
    return number


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    # No ``--help``: the only exit 0 is a ``clean`` verdict.
    parser = _Parser(
        prog="python -m src.ops.billing_fact_requeue",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="plan")
    parser.add_argument("--limit", type=_limit, default=None)
    args = parser.parse_args(list(argv))
    if args.limit is not None and args.command != "requeue":
        raise _UsageError()
    return args


def _settings_failure(command: str, error: BaseException) -> RunResult:
    """Settings that cannot load are ``unconfigured``; a broken image is an error."""

    if isinstance(error, ValidationError):
        fields = sorted(
            {
                location[0]
                for item in error.errors(
                    include_url=False, include_context=False, include_input=False
                )
                if (location := item.get("loc"))
                and isinstance(location[0], str)
                and _SETTINGS_FIELD.fullmatch(location[0])
            }
        )
        reasons = tuple(f"settings_invalid:{name}" for name in fields)
        return RunResult(
            command=command,
            verdict="unconfigured",
            reasons=reasons or ("settings_invalid",),
        )
    if type(error).__name__ == "ConfigurationError":
        return RunResult(
            command=command, verdict="unconfigured", reasons=("settings_invalid",)
        )
    return RunResult(command=command, verdict="error", reasons=("import_failed",))


async def _run_on_settings(
    command: str, settings: Settings, *, limit: int
) -> RunResult:
    engine = create_async_engine(
        settings.database_url, poolclass=NullPool, hide_parameters=True
    )
    try:
        return await run(
            command,
            async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
            limit=limit,
        )
    finally:
        await engine.dispose()


def _dumps(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def main(
    argv: Sequence[str] | None = None,
    *,
    sessions: Callable[[], AsyncSession] | None = None,
    settings: Settings | None = None,
    now: Callable[[], datetime] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the command; print one JSON line (stdout) and one log line (stderr).

    ``sessions`` (tests) replaces the engine built from ``DATABASE_URL``.
    """

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    started = time.monotonic()
    error_class: str | None = None
    result: RunResult
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except _UsageError:
        result = RunResult(command=None, verdict="unconfigured", reasons=("usage",))
    else:
        limit = args.limit if args.limit is not None else DEFAULT_LIMIT
        # Anything stray goes to stderr: stdout carries exactly one line.
        with contextlib.redirect_stdout(err):
            try:
                if _IMPORT_ERROR is not None:
                    result = _settings_failure(args.command, _IMPORT_ERROR)
                elif sessions is not None:
                    result = asyncio.run(
                        run(args.command, sessions, limit=limit, now=now)
                    )
                else:
                    # The process environment only: a ``.env`` in the
                    # working directory must not redirect the database.
                    source = (
                        settings
                        if settings is not None
                        # pydantic-settings' documented init option; mypy
                        # only sees the fields.
                        else Settings(_env_file=None)  # type: ignore[call-arg]
                    )
                    result = asyncio.run(
                        _run_on_settings(args.command, source, limit=limit)
                    )
            except ValidationError as exc:
                result = _settings_failure(args.command, exc)
            except SQLAlchemyError as exc:
                error_class = type(exc).__name__
                result = RunResult(
                    command=args.command,
                    verdict="error",
                    reasons=("database_error",),
                    limit=limit,
                )
            except Exception as exc:
                error_class = type(exc).__name__
                result = RunResult(
                    command=args.command,
                    verdict="error",
                    reasons=("unexpected",),
                    limit=limit,
                )
    document = result.document()
    out.write(_dumps(document) + "\n")
    out.flush()
    err.write(
        _dumps(
            {
                **document,
                "event": LOG_EVENT,
                "level": "info" if result.exit_code == 0 else "warning",
                "logger": LOGGER_NAME,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_class": error_class,
            }
        )
        + "\n"
    )
    err.flush()
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
