"""One-off operator task for the per-builder charge mark (cobranca-credito-por-builder).

The bridge release (PR-A) ships one subcommand, read-only::

    python -m src.ops.credit_charge_mark status --builder-id <uuid>

It runs as a one-off ECS task started from the service's own task
definition.  ``mark`` and ``unmark`` (the only writers of
``credit_enforcement_marks``) come with the behaviour release (PR-B); in this
release nothing writes a mark and no request path reads one, so the origin
``mark`` below describes what that release will apply, not what this image
does.

``status`` prints, for one builder:

* ``marks``: the latest event of ``credit_enforcement_marks`` (kind,
  ``occurred_at``, actor, reason) or ``no marks``; read only when the revision
  gate ``credit_switch.enforcement_mark_gate_open`` is open (the database at
  047 or later in this image's chain), else ``marks table absent`` and the
  table is not touched;
* ``enforcement``: the instant enforcement takes effect for the builder and
  its origin, in the order of the wish: the global ``credit_enforcement_state``
  row (``global``), else a latest event ``mark`` (``mark``, at its
  ``occurred_at``), else ``none``;
* ``lifecycle``: the open exhaustion episode and the open debt, read as the
  credit read routes read them (the stored rows, swept in memory at the
  database clock, never persisted);
* ``tenants``: every tenant whose ``tenants.builder_id`` is the builder, with
  its WhatsApp provider.  Only the JSON key ``whatsapp_provider`` is read
  from ``clinic_configs.api_credentials`` (``->>``), and only a known provider
  name is printed (anything else is ``unrecognized``): no credential can
  reach the output or the log;
* ``coverage``: ``CreditRolloutService.binding_coverage`` at the database
  clock (``usable``, ``reconciliation_failure``).

Guarantees:

1. The credit switch is re-read IN THIS PROCESS (``refresh_credit_switch``)
   and the command refuses unless it is effective (``switch_off``), reading
   nothing else.
2. Everything is read in one ``REPEATABLE READ READ ONLY`` transaction that
   is rolled back: the command can write nothing.
3. An unknown builder is ``refused`` (``builder_not_found``).

Output: exactly one JSON document on stdout, and on stderr the same document
plus the log fields (``event: credit_charge_mark_ops``), after any JSON log
line of the code it ran (``configure_task_logging``: JSON whatever
``LOG_FORMAT`` says, tracebacks without local variables).  Values are closed
vocabularies, identifiers (builder, tenant, episode and user UUIDs), instants,
amounts in millicredits and the reason an operator wrote with the mark.
Once PR-B writes marks, that operator-supplied reason is printed as stored to
stdout, stderr and the task logs: it must carry no personal data or secret
(PR-B's ``mark``/``unmark`` refuse control characters in it).
Exit codes are in ``EXIT_CODES``: 0 only for ``ok``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Callable, Final, Mapping, NoReturn, Sequence, TextIO
from uuid import UUID

# Every ``src`` import builds the global settings, which raise on an invalid
# environment.  The failure is kept so the command still prints its one JSON
# line instead of a traceback (the pattern of ``src.ops.asaas_webhook``).
try:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.config.settings import settings
    from src.models.credit import CreditAccount, CreditLot
    from src.repositories.content_retention import ContentRetentionRepository
    from src.repositories.credit import (
        CreditRepository,
        EnforcementMarkRow,
        TenantWhatsappProvider,
    )
    from src.services.api_credentials import PROVIDER_AVAILABILITY
    from src.services.credit_ledger import LEDGER_EFFECT_KINDS, assemble_book
    from src.services.credit_rollout import CreditRolloutService
    from src.services.credit_switch import (
        CreditSwitchReading,
        credit_switch_effective,
        enforcement_mark_gate_open,
        refresh_credit_switch,
    )
except Exception as exc:  # reported by ``amain`` as one JSON line
    _IMPORT_ERROR: BaseException | None = exc
else:
    _IMPORT_ERROR = None

LOGGER_NAME: Final = "src.ops.credit_charge_mark"
SCHEMA: Final = "neurostack.credit_charge_mark_ops.v1"
LOG_EVENT: Final = "credit_charge_mark_ops"
COMMANDS: Final = ("status",)
EXIT_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "ok": 0,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "error": 50,
    }
)
SUCCESS: Final[Mapping[str, str]] = MappingProxyType({"status": "ok"})
REASONS: Final = frozenset(
    {
        "usage",
        "import_failed",
        "settings_invalid",
        "credit_switch_off",
        "builder_not_found",
        "unexpected",
    }
)
# What ``marks.summary`` says.
MARKS_TABLE_ABSENT: Final = "marks table absent"
NO_MARKS: Final = "no marks"
ORIGINS: Final = ("global", "mark", "none")
# A tenant's provider as printed: a known WhatsApp provider name, ``None``
# (no config row, no key) or this word for any other stored value.
UNRECOGNIZED_PROVIDER: Final = "unrecognized"
_READ_ONLY_SQL: Final = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

SessionFactory = Callable[[], "AsyncSession"]


def known_whatsapp_providers() -> frozenset[str]:
    """The provider names ``status`` prints as they are (every WhatsApp
    provider of the public availability matrix)."""

    return frozenset(PROVIDER_AVAILABILITY["whatsapp"])


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResult:
    """The one document a run prints; validated against closed vocabularies."""

    command: str | None
    verdict: str
    reasons: tuple[str, ...] = ()
    switch: Mapping[str, object] | None = None
    builder_id: str | None = None
    database_now: str | None = None
    builder: Mapping[str, object] | None = None
    marks: Mapping[str, object] | None = None
    enforcement: Mapping[str, object] | None = None
    lifecycle: Mapping[str, object] | None = None
    tenants: Sequence[Mapping[str, object]] | None = None
    coverage: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.verdict not in EXIT_CODES:
            raise ValueError("verdict outside the closed vocabulary")
        if self.command is not None and self.command not in COMMANDS:
            raise ValueError("command outside the closed vocabulary")
        if EXIT_CODES[self.verdict] == 0 and (
            self.command is None or SUCCESS[self.command] != self.verdict
        ):
            raise ValueError("a success verdict of another command")
        for reason in self.reasons:
            if reason not in REASONS:
                raise ValueError("reason outside the closed vocabulary")
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "command": self.command,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "exit_code": self.exit_code,
            "switch": None if self.switch is None else dict(self.switch),
            "builder_id": self.builder_id,
            "database_now": self.database_now,
            "builder": None if self.builder is None else dict(self.builder),
            "marks": None if self.marks is None else dict(self.marks),
            "enforcement": None if self.enforcement is None else dict(self.enforcement),
            "lifecycle": None if self.lifecycle is None else dict(self.lifecycle),
            "tenants": (
                None if self.tenants is None else [dict(row) for row in self.tenants]
            ),
            "coverage": None if self.coverage is None else dict(self.coverage),
        }


def _iso(value: datetime | None) -> str | None:
    """UTC ISO 8601 (a naive value, as SQLite returns it, is UTC)."""

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pure formatting
# ---------------------------------------------------------------------------


def provider_label(raw: str | None) -> str | None:
    """The printable provider of one tenant: never a stored value outside the
    known names, so a credential written under the key cannot be printed."""

    if raw is None:
        return None
    return raw if raw in known_whatsapp_providers() else UNRECOGNIZED_PROVIDER


def marks_document(
    *, gate_open: bool, latest: EnforcementMarkRow | None
) -> dict[str, object]:
    """The marks part.  With the 047 gate closed the database is below 047,
    where the table does not exist (047 creates it), and it was not read."""

    if not gate_open:
        return {"gate_open": False, "summary": MARKS_TABLE_ABSENT, "latest": None}
    if latest is None:
        return {"gate_open": True, "summary": NO_MARKS, "latest": None}
    return {
        "gate_open": True,
        "summary": f"latest {latest.kind}",
        "latest": {
            "id": str(latest.id),
            "kind": latest.kind,
            "occurred_at": _iso(latest.occurred_at),
            "actor_user_id": str(latest.actor_user_id),
            "reason": latest.reason,
        },
    }


def enforcement_document(
    global_at: datetime | None, latest: EnforcementMarkRow | None
) -> dict[str, object]:
    """Global first, then a latest ``mark``, else none (the wish's order)."""

    if global_at is not None:
        effective_at: datetime | None = global_at
        origin = "global"
    elif latest is not None and latest.kind == "mark":
        effective_at, origin = latest.occurred_at, "mark"
    else:
        effective_at, origin = None, "none"
    return {
        "global_at": _iso(global_at),
        "effective_at": _iso(effective_at),
        "origin": origin,
    }


def tenants_document(rows: Sequence[TenantWhatsappProvider]) -> list[dict[str, object]]:
    return [
        {
            "tenant_id": str(row.tenant_id),
            "whatsapp_provider": provider_label(row.whatsapp_provider),
        }
        for row in rows
    ]


def _switch_document(reading: CreditSwitchReading) -> dict[str, object]:
    return {
        "effective": reading.effective,
        "reason": reading.reason,
        "revision": reading.revision,
    }


# ---------------------------------------------------------------------------
# Database reads (one read-only transaction, rolled back)
# ---------------------------------------------------------------------------


async def _read_only(session: AsyncSession) -> None:
    """The first statement of the transaction: nothing after it can write."""

    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(_READ_ONLY_SQL))


async def _open_debt(
    session: AsyncSession, builder_id: UUID, now: datetime
) -> tuple[int, int]:
    """``(open debt in mc, open debts)`` from the stored rows, swept at *now*
    in memory: the figure ``/v1/credits/debt`` reads (no lock, no write)."""

    repository = CreditRepository(session)
    account_id = await session.scalar(
        select(CreditAccount.id).where(CreditAccount.builder_id == builder_id)
    )
    lots = (
        (
            await session.execute(
                select(CreditLot)
                .where(CreditLot.builder_id == builder_id)
                .order_by(CreditLot.id)
            )
        )
        .scalars()
        .all()
    )
    lot_columns = (
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
    debt_columns = (
        "id",
        "created_at",
        "debt_source_kind",
        "debt_source_effect_id",
        "original_grant_lot_id",
        "amount_mc",
    )
    book = assemble_book(
        builder_id=builder_id,
        # A builder that never had a credit row has no account: an empty
        # book (no lot can exist without one).
        account_id=account_id if account_id is not None else builder_id,
        lots=[{column: getattr(lot, column) for column in lot_columns} for lot in lots],
        lot_sums=await repository.lot_entry_sums(builder_id),
        debts=[
            {column: getattr(debt, column) for column in debt_columns}
            for debt in await repository.debts(builder_id)
        ],
        settled=await repository.settlement_sums(builder_id),
        known_effects=await repository.effect_ids_of_kinds(
            builder_id, LEDGER_EFFECT_KINDS
        ),
        release_rows=await repository.release_sums(builder_id),
        ledger_debt_mc=(await repository.derived_balance(builder_id)).debt_mc,
        clock_floor=await repository.latest_occurred_at(builder_id),
    )
    book.sweep_expired(now)
    open_by_id = book.open_debt_by_id()
    return book.open_debt_mc, sum(1 for value in open_by_id.values() if value > 0)


async def _status(
    args: argparse.Namespace,
    sessions: SessionFactory,
    reading: CreditSwitchReading,
    switch: dict[str, object],
) -> RunResult:
    builder_id: UUID = args.builder_id
    gate_open = enforcement_mark_gate_open(reading)
    async with sessions() as session:
        try:
            await _read_only(session)
            repository = CreditRepository(session)
            now = await repository.database_now()
            active = await repository.builder_is_active(builder_id)
            if active is None:
                return RunResult(
                    command="status",
                    verdict="refused",
                    reasons=("builder_not_found",),
                    switch=switch,
                    builder_id=str(builder_id),
                    database_now=_iso(now),
                )
            # Below 047 (the gate closed) the table does not exist and is not
            # touched; at 047 or later it is read.
            latest = (
                await repository.latest_enforcement_mark(builder_id)
                if gate_open
                else None
            )
            global_at = await repository.enforcement_at()
            episode = await ContentRetentionRepository(session).open_episode(builder_id)
            # Copied before the rollback, which expires every loaded entity.
            open_episode = (
                None
                if episode is None
                else {
                    "id": str(episode.id),
                    "exhausted_at": _iso(episode.exhausted_at),
                    "suspension_due_at": _iso(episode.suspension_due_at),
                    "deletion_due_at": _iso(episode.deletion_due_at),
                }
            )
            open_debt_mc, open_debts = await _open_debt(session, builder_id, now)
            tenants = await repository.builder_tenant_whatsapp_providers(builder_id)
            coverage = await CreditRolloutService(session).binding_coverage(
                builder_id, at=now
            )
        finally:
            await session.rollback()
    return RunResult(
        command="status",
        verdict="ok",
        switch=switch,
        builder_id=str(builder_id),
        database_now=_iso(now),
        builder={"exists": True, "active": active},
        marks=marks_document(gate_open=gate_open, latest=latest),
        enforcement=enforcement_document(global_at, latest),
        lifecycle={
            "open_episode": open_episode,
            "open_debt_mc": open_debt_mc,
            "open_debts": open_debts,
        },
        tenants=tenants_document(tenants),
        coverage={
            "usable": coverage.usable,
            "reconciliation_failure": coverage.reconciliation_failure,
        },
    )


async def run(
    args: argparse.Namespace, *, session_factory: SessionFactory | None = None
) -> RunResult:
    """Run one parsed command; owns the process pool unless one is injected."""

    owned = session_factory is None
    sessions = session_factory
    try:
        if owned and settings.credit_hot_path_enabled is True:
            from src.repositories.database import get_db_session_factory, init_database

            await init_database()
            sessions = get_db_session_factory()
        # Re-read the switch here: the verdict is per process and nothing
        # else refreshes it in a one-off task.  Off, it reads nothing.
        reading = await refresh_credit_switch(session_factory=sessions)
        switch = _switch_document(reading)
        if not (reading.effective and credit_switch_effective()):
            return RunResult(
                command=args.command,
                verdict="switch_off",
                reasons=("credit_switch_off",),
                switch=switch,
                builder_id=str(args.builder_id),
            )
        if sessions is None:  # effective implies a database was read
            raise RuntimeError("no session factory for an effective switch")
        return await _status(args, sessions, reading, switch)
    finally:
        if owned:
            from src.repositories.database import close_database

            await close_database()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError()


def _uuid(value: str) -> UUID:
    """Only the canonical lowercase hyphenated form."""

    try:
        parsed = UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("uuid") from error
    if str(parsed) != value:
        raise argparse.ArgumentTypeError("uuid")
    return parsed


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse one invocation; anything malformed raises ``_UsageError``."""

    # No ``--help``: the only exit 0 is a command's success verdict.
    parser = _Parser(
        prog="python -m src.ops.credit_charge_mark", add_help=False, allow_abbrev=False
    )
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    status = commands.add_parser("status", add_help=False, allow_abbrev=False)
    status.add_argument("--builder-id", type=_uuid, required=True)
    return parser.parse_args(list(argv))


def _import_failure(command: str, error: BaseException) -> RunResult:
    """Settings that cannot load are ``unconfigured``; a broken image is an error."""

    if type(error).__name__ in {"ValidationError", "ConfigurationError"}:
        return RunResult(
            command=command, verdict="unconfigured", reasons=("settings_invalid",)
        )
    return RunResult(command=command, verdict="error", reasons=("import_failed",))


def _dumps(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


async def amain(
    argv: Sequence[str] | None = None,
    *,
    session_factory: SessionFactory | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one invocation; print one document (stdout) and one log line (stderr)."""

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    started = time.monotonic()
    error_class: str | None = None
    result: RunResult
    try:
        args = parse_arguments(sys.argv[1:] if argv is None else argv)
    except _UsageError:
        result = RunResult(command=None, verdict="unconfigured", reasons=("usage",))
    else:
        try:
            if _IMPORT_ERROR is not None:
                result = _import_failure(args.command, _IMPORT_ERROR)
            else:
                result = await run(args, session_factory=session_factory)
        except Exception as exc:
            error_class = type(exc).__name__
            result = RunResult(
                command=args.command, verdict="error", reasons=("unexpected",)
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


def configure_task_logging(stream: TextIO) -> None:
    """JSON log lines on *stream*, whatever ``LOG_FORMAT`` says.

    The same configuration as the L10 arming task (kept apart on purpose:
    no module imports that task).  The API's ``configure_logging`` is not
    used: with ``LOG_FORMAT=console`` it renders exceptions through
    structlog's console renderer, which prints a traceback's local variables
    when ``rich`` is importable -- among them the database URL with its
    password (``init_database`` logs its failure with ``exc_info``).
    ``format_exc_info`` renders the plain traceback: file, line and source,
    no locals.
    """

    import logging

    import structlog

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(
        format="%(message)s", stream=stream, level=logging.INFO, force=True
    )
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> int:
    """The process entry point: anything stray goes to stderr, so stdout
    carries exactly the one document."""

    out, err = sys.stdout, sys.stderr
    configure_task_logging(err)
    with contextlib.redirect_stdout(err):
        return asyncio.run(amain(argv, stdout=out, stderr=err))


if __name__ == "__main__":
    raise SystemExit(main())
