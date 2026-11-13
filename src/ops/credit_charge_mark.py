"""One-off operator task for the per-builder charge mark (cobranca-credito-por-builder).

Three subcommands, and no other way in (no HTTP route, worker or script
imports this module)::

    python -m src.ops.credit_charge_mark status --builder-id <uuid>
    python -m src.ops.credit_charge_mark mark --builder-id <uuid> \\
        --author-user-id <uuid> --reason "<why>"
    python -m src.ops.credit_charge_mark unmark --builder-id <uuid> \\
        --author-user-id <uuid> --reason "<why>"

It runs as a one-off ECS task started from the service's own task
definition.  ``mark`` and ``unmark`` are the only writers of
``credit_enforcement_marks``, through
``CreditRepository.insert_enforcement_mark`` (this module is that method's
one caller); the request path reads the mark through
``CreditRepository.effective_enforcement`` -- the same reader ``status``
prints below.

``status`` prints, for one builder:

* ``marks``: the latest event of ``credit_enforcement_marks`` (kind,
  ``occurred_at``, actor, reason) or ``no marks``; read only when the revision
  gate ``credit_switch.enforcement_mark_gate_open`` is open (the database at
  047 or later in this image's chain), else ``marks table absent`` and the
  table is not touched;
* ``enforcement``: the instant enforcement takes effect for the builder and
  its origin, in the order of the wish: the global ``credit_enforcement_state``
  row (``global``), else a latest event ``mark`` (``mark``, at its
  ``occurred_at``), else -- debito-cortesia-automatico -- the courtesy rule
  (``courtesy``, at the builder's first courtesy grant, with ``state``
  ``charged`` while courtesy is spendable or ``blocked`` with none left and
  no paid plan in force; behind the 046 gate, judged at the database
  clock; ``state`` is printed under ``courtesy`` only), else ``none`` -- read by
  ``CreditRepository.effective_enforcement``, the request path's own reader
  (one implementation of the rule);
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

``mark`` and ``unmark`` each append ONE event (kind, the database clock,
``--author-user-id``, ``--reason``) in one read-write transaction, or refuse
and write nothing (the transaction is rolled back).  In that transaction, in
this order (the lock order of ``src.repositories.credit``, extended by two
rows no request path locks before an account):

1. the author's ``users`` row ``FOR SHARE``: it must exist, be active and be a
   ``super_admin`` (the L10 task's author rule, same row, same lock, same
   reason code), else ``author_not_active_super_admin``;
2. the builder's ``builders`` row ``FOR KEY SHARE`` (the marks table has no
   foreign key, so this is the existence check: a builder deleted
   concurrently either commits first and is ``builder_not_found``, or waits
   for this commit), else ``builder_not_found``;
3. the builder's credit account ``FOR UPDATE`` -- ``ensure_account`` then
   ``lock_account``, exactly as the request path takes it
   (``credit_metering.require_captured_enforcement``, lock order step 3).
   It serializes two commands of one builder (``insert_enforcement_mark``
   reads the latest instant, then inserts) and an ``unmark`` with the
   enforced requests of the builder.  A builder that never had an account
   gets its empty account in a command that succeeds (the request path's own
   on-demand creation); a refusal rolls it back with everything else;
4. the global ``credit_enforcement_at``: once the L10 is armed every builder
   is charged and a mark is redundant, so both commands refuse
   (``global_enforcement_armed``).  This read takes no L10 rollout lock: an
   L10 arm that commits while a command runs is harmless, because the global
   always wins over a mark;
5. the builder's latest event: ``mark`` refuses an already marked builder
   (``already_marked``, no duplicate event), ``unmark`` a builder whose
   latest event is not a ``mark`` (``not_marked``).

Then ``mark`` refuses, reporting every one that applies:

* ``coverage_unusable``: ``binding_coverage(...).usable`` is false at the
  database clock (an ASAAS quarantine, an identity conflict or a
  ``reconciliation_failure``): enforced, every request would be
  ``credit_pending``;
* ``channel_not_allowed``: a tenant linked to the builder has a
  ``whatsapp_provider`` outside :data:`ALLOWED_WHATSAPP_PROVIDERS` (``zapi``,
  ``meta_cloud``) other than none (no config row, no key, JSON null).
  ``uazapi``, ``evolution``, ``dialog360``, ``twilio``, an empty string and
  any unknown value are refused: those channels have no enforced credit
  path.  Only ``whatsapp_provider`` is read (``->>``), and only a known name
  is printed.  Accepted as partial charging (documented in the wish): a
  clinic linked to the builder after the ``mark``, and a tenant holding
  Uazapi/Evolution keys with no ``whatsapp_provider``;
* ``window_already_opened``: the current accounting window already has an
  opening (``CreditRepository.mark_window_state``: any ``baseline`` or
  ``maintenance`` allocation of the window, whatever opened it).  With the
  global unset an opening comes from an earlier mark or -- debito-cortesia-
  automatico, accepted by its design -- from the courtesy rule (a builder
  under it opens its windows as enforced traffic); a mark in that window
  would charge what was loaded while not marked.  The next window admits
  it: a ``mark`` of a builder whose enforced traffic (an earlier mark's or
  the rule's) opened the current window is refused as
  ``window_already_opened`` until the next civil month (the next
  accounting window).

And ``unmark`` refuses, read under the account lock of step 3:

* ``open_episode``: the builder has an open exhaustion episode;
* ``open_debt``: the builder has open debt (the stored rows swept at the
  database clock, as ``status`` reads it).

A request that captured the mark before the ``unmark`` and already holds,
or is queued on, the account lock may still debit after the ``unmark``
commits (it re-checks the mark under that lock only once it has it): the
``unmark`` waits for a holder, and a request queued behind it sees the
``unmark``; one admitted in between debits once more.  The way out of a
charge left that way is the usual one (a courtesy, then ``unmark``).

With the 047 gate closed (database below 047) both commands refuse
(``marks_table_absent``) without touching the table.

Guarantees:

1. The credit switch is re-read IN THIS PROCESS (``refresh_credit_switch``)
   and the command refuses unless it is effective (``switch_off``), reading
   nothing else.
2. ``status`` reads everything in one ``REPEATABLE READ READ ONLY``
   transaction that is rolled back: it can write nothing.
3. An unknown builder is ``refused`` (``builder_not_found``).
4. ``mark``/``unmark`` write one event or nothing; ``occurred_at`` is the
   database clock, kept strictly increasing per builder by the writer.

``--reason`` is required: trimmed, non-empty, at most
:data:`REASON_MAX_LENGTH` characters, one printable line (no control or
format character, no invisible filler), with at least one letter or digit,
and nothing that looks like an e-mail address; anything else is ``usage``.
Invisible combining marks next to real text (such as U+034F) are still
accepted: cosmetic, the reason stays readable.  It is stored with the event
and printed as given to stdout, stderr and the task logs, and the table is
append-only: it must carry no personal data and no secret (the runbook says
so).

Output: exactly one JSON document on stdout, and on stderr the same document
plus the log fields (``event: credit_charge_mark_ops``), after any JSON log
line of the code it ran (``configure_task_logging``: JSON whatever
``LOG_FORMAT`` says, tracebacks without local variables).  Values are closed
vocabularies, identifiers (builder, tenant, episode, event and user UUIDs),
instants, amounts in millicredits and the reason an operator wrote with a
mark.
Exit codes are in ``EXIT_CODES``: 0 only for the command's own success
verdict (``ok``, ``marked``, ``unmarked``); every refusal is ``refused``
(20).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, NoReturn, Sequence, TextIO
from uuid import UUID

# Every ``src`` import builds the global settings, which raise on an invalid
# environment.  The failure is kept so the command still prints its one JSON
# line instead of a traceback (the pattern of ``src.ops.asaas_webhook``).
try:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.config.settings import settings
    from src.models.builder import Builder
    from src.models.credit import CreditAccount, CreditLot
    from src.models.user import User, UserRole
    from src.repositories.content_retention import ContentRetentionRepository
    from src.repositories.credit import (
        CreditRepository,
        EffectiveEnforcement,
        EnforcementMarkRow,
        MarkWindowState,
        TenantWhatsappProvider,
    )
    from src.services.api_credentials import PROVIDER_AVAILABILITY
    from src.services.credit_ledger import LEDGER_EFFECT_KINDS, assemble_book
    from src.services.credit_rollout import CreditRolloutService
    from src.services.credit_switch import (
        CreditSwitchReading,
        courtesy_rule_gate_open,
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
COMMANDS: Final = ("status", "mark", "unmark")
EXIT_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "ok": 0,
        "marked": 0,
        "unmarked": 0,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "error": 50,
    }
)
SUCCESS: Final[Mapping[str, str]] = MappingProxyType(
    {"status": "ok", "mark": "marked", "unmark": "unmarked"}
)
REASONS: Final = frozenset(
    {
        "usage",
        "import_failed",
        "settings_invalid",
        "credit_switch_off",
        "builder_not_found",
        "unexpected",
        # mark / unmark refusals (module docstring).
        "author_not_active_super_admin",
        "marks_table_absent",
        "global_enforcement_armed",
        "already_marked",
        "not_marked",
        "coverage_unusable",
        "channel_not_allowed",
        "window_already_opened",
        "open_episode",
        "open_debt",
    }
)
# The channels a marked builder's tenants may use: the ones with an enforced
# credit path.  A tenant with no provider (``None``) is admitted too.
ALLOWED_WHATSAPP_PROVIDERS: Final = frozenset({"zapi", "meta_cloud"})
# ``credit_enforcement_marks.reason`` is ``VARCHAR(500)`` with a non-blank
# CHECK (migration 047, ``ENFORCEMENT_MARK_REASON_MAX_LENGTH``).
REASON_MAX_LENGTH: Final = 500
# An e-mail-like token (the L10 task's rationale rule, copied: this module
# never imports that task): personal data the append-only table must not keep.
_EMAIL_LIKE: Final = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
# Printable but invisible fillers (Hangul fillers count as letters for
# ``str.isalnum``; the Braille blank is a symbol): refused anywhere in a
# reason, so a blank-looking reason cannot pass the letter-or-digit rule.
_INVISIBLE_FILLERS: Final = frozenset("\u115f\u1160\u3164\uffa0\u2800")
# What ``marks.summary`` says.
MARKS_TABLE_ABSENT: Final = "marks table absent"
NO_MARKS: Final = "no marks"
ORIGINS: Final = ("global", "mark", "courtesy", "none")
# ``enforcement.state``: the courtesy rule's, printed under ``courtesy`` only.
COURTESY_STATES: Final = ("charged", "blocked")
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
    # ``mark``/``unmark`` only: the author, the reason, the window state and
    # the event written (``None`` when nothing was written).
    change: Mapping[str, object] | None = None

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
            "change": None if self.change is None else dict(self.change),
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


def enforcement_document(effective: EffectiveEnforcement) -> dict[str, object]:
    """The builder's :class:`~src.repositories.credit.EffectiveEnforcement`
    as printed.  The rule itself (global first, then a latest ``mark``, then
    the courtesy rule, else none) is ``CreditRepository.effective_enforcement``'s,
    the request path's own reader: this only formats it.  ``global_at`` is
    the global instant, known exactly when it decided (it always wins).
    Under ``courtesy`` only, ``state`` is the rule's (``charged`` or
    ``blocked``); every other origin prints exactly the three keys it
    printed before the rule existed."""

    origin = "none" if effective.origin is None else effective.origin
    if origin not in ORIGINS:
        raise ValueError("origin outside the closed vocabulary")
    document: dict[str, object] = {
        "global_at": _iso(effective.at if effective.origin == "global" else None),
        "effective_at": _iso(effective.at),
        "origin": origin,
    }
    if origin == "courtesy":
        if effective.state not in COURTESY_STATES:
            raise ValueError("courtesy state outside the closed vocabulary")
        document["state"] = effective.state
    return document


def tenants_document(rows: Sequence[TenantWhatsappProvider]) -> list[dict[str, object]]:
    return [
        {
            "tenant_id": str(row.tenant_id),
            "whatsapp_provider": provider_label(row.whatsapp_provider),
        }
        for row in rows
    ]


def channel_allowed(raw: str | None) -> bool:
    """Whether a tenant's stored ``whatsapp_provider`` admits a ``mark``:
    none at all, or one of :data:`ALLOWED_WHATSAPP_PROVIDERS` exactly."""

    return raw is None or raw in ALLOWED_WHATSAPP_PROVIDERS


def event_document(row: EnforcementMarkRow) -> dict[str, object]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "occurred_at": _iso(row.occurred_at),
        "actor_user_id": str(row.actor_user_id),
        "reason": row.reason,
    }


def window_document(state: MarkWindowState) -> dict[str, object]:
    latest = state.latest_mark
    return {
        "window_start": state.window_start.isoformat(),
        "window_opened": state.window_opened,
        "latest_mark_at": None if latest is None else _iso(latest.occurred_at),
    }


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
    rule_readable = courtesy_rule_gate_open(reading)
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
            # touched; at 047 or later it is read.  Both reads take this
            # command's own gate verdict, never the process cache's -- as
            # does the courtesy rule (its 046 gate), judged at the database
            # clock.
            latest = (
                await repository.latest_enforcement_mark(
                    builder_id, marks_readable=True
                )
                if gate_open
                else None
            )
            effective = await repository.effective_enforcement(
                builder_id,
                marks_readable=gate_open,
                rule_readable=rule_readable,
                at=now,
            )
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
        enforcement=enforcement_document(effective),
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


# ---------------------------------------------------------------------------
# mark / unmark (one read-write transaction: one event or nothing)
# ---------------------------------------------------------------------------


async def _author_is_active_super_admin(session: AsyncSession, user_id: UUID) -> bool:
    """The L10 task's author rule (the cutoff acceptance's): the row exists,
    is active and is a ``super_admin``; held ``FOR SHARE`` until the
    commit, so a concurrent deactivation waits for this command."""

    row = (
        await session.execute(
            select(User.is_active, User.role)
            .where(User.id == user_id)
            .with_for_update(read=True)
        )
    ).one_or_none()
    return (
        row is not None and row.is_active is True and row.role == UserRole.super_admin
    )


async def _builder_exists(session: AsyncSession, builder_id: UUID) -> bool:
    """The builder row ``FOR KEY SHARE``: the marks table has no foreign key,
    so a deletion either committed before (``False``) or waits for this
    transaction."""

    found = (
        await session.execute(
            select(Builder.id)
            .where(Builder.id == builder_id)
            .with_for_update(read=True, key_share=True)
        )
    ).scalar_one_or_none()
    return found is not None


async def _change(
    args: argparse.Namespace,
    sessions: SessionFactory,
    reading: CreditSwitchReading,
    switch: dict[str, object],
) -> RunResult:
    """``mark`` or ``unmark`` (``args.command``): the steps and refusals of
    the module docstring, in its order."""

    command: str = args.command
    builder_id: UUID = args.builder_id
    change: dict[str, object] = {
        "kind": command,
        "author_user_id": str(args.author_user_id),
        "reason": args.reason,
        "window": None,
        "event": None,
    }

    def result(verdict: str, *reasons: str, **fields: Any) -> RunResult:
        return RunResult(
            command=command,
            verdict=verdict,
            reasons=reasons,
            switch=switch,
            builder_id=str(builder_id),
            change=dict(change),
            **fields,
        )

    if not enforcement_mark_gate_open(reading):
        # Below 047: the table does not exist and is not touched.
        return result("refused", "marks_table_absent")
    rule_readable = courtesy_rule_gate_open(reading)
    async with sessions() as session:
        try:
            repository = CreditRepository(session)
            if not await _author_is_active_super_admin(session, args.author_user_id):
                await session.rollback()
                return result("refused", "author_not_active_super_admin")
            if not await _builder_exists(session, builder_id):
                await session.rollback()
                return result("refused", "builder_not_found")
            # Lock order step 3, as the request path takes it.
            await repository.ensure_account(builder_id)
            await repository.lock_account(builder_id)
            now = await repository.database_now()
            if await repository.enforcement_at() is not None:
                effective = await repository.effective_enforcement(
                    builder_id, marks_readable=True, rule_readable=rule_readable, at=now
                )
                await session.rollback()
                return result(
                    "refused",
                    "global_enforcement_armed",
                    database_now=_iso(now),
                    enforcement=enforcement_document(effective),
                )
            latest = await repository.latest_enforcement_mark(
                builder_id, marks_readable=True
            )
            marked = latest is not None and latest.kind == "mark"
            context: dict[str, Any] = {
                "database_now": _iso(now),
                "marks": marks_document(gate_open=True, latest=latest),
            }
            if command == "mark" and marked:
                await session.rollback()
                return result("refused", "already_marked", **context)
            if command == "unmark" and not marked:
                await session.rollback()
                return result("refused", "not_marked", **context)
            refusals: list[str] = []
            if command == "mark":
                coverage = await CreditRolloutService(session).binding_coverage(
                    builder_id, at=now
                )
                tenants = await repository.builder_tenant_whatsapp_providers(builder_id)
                window = await repository.mark_window_state(
                    builder_id, at=now, marks_readable=True
                )
                change["window"] = window_document(window)
                context["coverage"] = {
                    "usable": coverage.usable,
                    "reconciliation_failure": coverage.reconciliation_failure,
                }
                context["tenants"] = tenants_document(tenants)
                if not coverage.usable:
                    refusals.append("coverage_unusable")
                if not all(channel_allowed(row.whatsapp_provider) for row in tenants):
                    refusals.append("channel_not_allowed")
                if window.window_opened:
                    refusals.append("window_already_opened")
            else:
                episode = await ContentRetentionRepository(session).open_episode(
                    builder_id
                )
                open_episode = None if episode is None else str(episode.id)
                open_debt_mc, open_debts = await _open_debt(session, builder_id, now)
                context["lifecycle"] = {
                    "open_episode": open_episode,
                    "open_debt_mc": open_debt_mc,
                    "open_debts": open_debts,
                }
                if open_episode is not None:
                    refusals.append("open_episode")
                if open_debt_mc > 0:
                    refusals.append("open_debt")
            if refusals:
                await session.rollback()
                return result("refused", *refusals, **context)
            written = await repository.insert_enforcement_mark(
                builder_id=builder_id,
                kind=command,
                actor_user_id=args.author_user_id,
                reason=args.reason,
                marks_readable=True,
            )
            effective = await repository.effective_enforcement(
                builder_id, marks_readable=True, rule_readable=rule_readable, at=now
            )
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
    change["event"] = event_document(written)
    return result(
        SUCCESS[command],
        **context,
        enforcement=enforcement_document(effective),
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
        if args.command == "status":
            return await _status(args, sessions, reading, switch)
        return await _change(args, sessions, reading, switch)
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


def _reason(value: str) -> str:
    """Trimmed, non-empty, at most :data:`REASON_MAX_LENGTH` characters and
    one printable line: ``str.isprintable`` refuses every control, format,
    separator-other-than-space and unassigned character (newline, tab,
    ``\\x1b``, zero-width space, bidi overrides).  It must also hold at least
    one letter or digit, no printable invisible filler (U+115F, U+1160,
    U+3164, U+FFA0, U+2800: a reason that looks blank says nothing) and no
    e-mail-like token (no personal data in an append-only table)."""

    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > REASON_MAX_LENGTH
        or not stripped.isprintable()
        or not any(character.isalnum() for character in stripped)
        or any(character in _INVISIBLE_FILLERS for character in stripped)
        or _EMAIL_LIKE.search(stripped)
    ):
        raise argparse.ArgumentTypeError("reason")
    return stripped


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
    for name in ("mark", "unmark"):
        change = commands.add_parser(name, add_help=False, allow_abbrev=False)
        change.add_argument("--builder-id", type=_uuid, required=True)
        change.add_argument("--author-user-id", type=_uuid, required=True)
        change.add_argument("--reason", type=_reason, required=True)
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
