"""One-off task that arms the credit enforcement (asaas-rollout-opening G6).

``CreditRolloutService.arm_enforcement`` and ``accept_cutoff`` have no route.
This module is their only caller.  It runs as a one-off ECS task started from
the service's own task definition, and only on the owner's explicit order for
that step (Decisions 6, 7 and 11)::

    python -m src.ops.credit_enforcement status
    python -m src.ops.credit_enforcement report
    python -m src.ops.credit_enforcement accept --report-digest <sha256> \\
        --tenant-id <uuid> --builder-id <uuid|none> --reason-code <code> \\
        --author-user-id <uuid> --rationale "<the owner's written reason>"
    python -m src.ops.credit_enforcement arm --report-digest <sha256>

The procedure is ``docs/runbooks/credit-enforcement-arming.md``.  Arming is
irreversible: no code clears ``credit_enforcement_at``.  The containment is
the credit switch (``CREDIT_HOT_PATH_ENABLED=false``).

Subcommands.

``status`` (read-only)
    The switch verdict, the ``credit_enforcement_state`` row, readiness, the
    open orphans by class, what ``arm``'s own first step (closing the
    effects of deleted tenants) would write and leave open, and the cutoff
    digest with its finding and acceptance counts.
``report`` (read-only)
    The tenant cutoff report at the database clock: its digest and each
    finding ``(tenant_id, builder_id, reason)``, with whether an acceptance
    for THIS digest exists.  The owner reviews it tenant by tenant.
``accept``
    Records the owner's acceptance of one finding of the CURRENT report.
    The finding is named by its ``(tenant, builder, reason code)`` triple,
    because that triple is what the report matches on.  It must be a
    finding of the report evaluated now, whose digest must equal
    ``--report-digest``.  The author must be an active ``super_admin``
    (checked by ``accept_cutoff``), and the instant is the database clock.
    The rationale is NOT stored (no column exists for it).  It goes into
    this task's output and log, and into the owner's record; it must carry
    no personal data (an e-mail-like string is refused as ``usage``).
    Refused once armed: the accept transaction takes the rollout lock that
    ``arm_enforcement`` takes and re-reads ``credit_enforcement_at`` under it.
``arm``
    ``arm_enforcement(set_by=<this task's ARN>, at=None,
    expected_report_digest=<--report-digest>)``.  The digest is compared
    inside the arming transaction, under the rollout lock, against the
    report that transaction evaluates.  ``at=None`` means the database
    clock.

Guarantees, in order, for every subcommand:

1. The credit switch is re-read IN THIS PROCESS (``refresh_credit_switch``)
   and the command refuses unless it is effective.  ``credit_switch_effective``
   is per process and only the API lifespan refreshes it, and
   ``arm_enforcement`` does not consult it.
2. ``status`` and ``report`` run in a ``REPEATABLE READ READ ONLY``
   transaction that is rolled back, so they can write nothing.
3. ``accept`` and ``arm`` need this task's ARN, read from the ECS task
   metadata endpoint (``ECS_CONTAINER_METADATA_URI_V4``).  Without it they
   refuse (``task_identity_unavailable``).  ``arm`` stores it in ``set_by``.
   ``accept`` logs it, since the acceptance has no such column.
4. After a commit the row is read back in a FRESH session and compared with
   what the transaction wrote (``readback_mismatch`` otherwise).
5. "Already armed" has its own verdict and exit code.  It is checked before
   the transaction and again after any refusal, so the loser of two
   concurrent arms also reports it.

Output: exactly one JSON document on stdout, and on stderr the same document
plus the log fields (``event: credit_enforcement_ops``), after any JSON log
line of the code it ran (``configure_task_logging``: JSON whatever
``LOG_FORMAT`` says, tracebacks without local variables).  Values come from
closed vocabularies, identifiers (tenant, builder and user UUIDs, the task
ARN), digests, instants and the owner's rationale.  There are no secrets
and no PII.  Exit codes are in ``EXIT_CODES``: 0 only for the command's own
success verdict, and ``already_armed`` is 10.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Final, Mapping, NoReturn, Sequence, TextIO
from urllib.parse import urlsplit
from uuid import UUID

import httpx

# Every ``src`` import builds the global settings, which raise on an invalid
# environment.  The failure is kept so the command still prints its one JSON
# line instead of a traceback (the pattern of ``src.ops.asaas_webhook``).
try:
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.config.settings import settings
    from src.models.credit import ENFORCEMENT_STATE_ID, CreditEnforcementState
    from src.models.credit_enforcement import CreditCutoffAcceptance
    from src.repositories.credit import CreditRepository
    from src.services.credit_rollout import (
        CreditEnforcementRejected,
        CreditRolloutService,
        CutoffReport,
        OrphanReport,
    )
    from src.services.credit_switch import (
        CreditSwitchReading,
        credit_switch_effective,
        refresh_credit_switch,
    )
except Exception as exc:  # reported by ``amain`` as one JSON line
    _IMPORT_ERROR: BaseException | None = exc
else:
    _IMPORT_ERROR = None

LOGGER_NAME: Final = "src.ops.credit_enforcement"
SCHEMA: Final = "neurostack.credit_enforcement_ops.v1"
LOG_EVENT: Final = "credit_enforcement_ops"
COMMANDS: Final = ("status", "report", "accept", "arm")
EXIT_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "ok": 0,
        "accepted": 0,
        "armed": 0,
        "already_armed": 10,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "readback_mismatch": 40,
        "error": 50,
    }
)
# The one success verdict of each command (exit code 0).
SUCCESS: Final[Mapping[str, str]] = MappingProxyType(
    {"status": "ok", "report": "ok", "accept": "accepted", "arm": "armed"}
)
# The reason codes ``CreditRolloutService.cutoff_report`` emits (pinned by
# tests/ops/test_credit_enforcement.py against that function's literals).
CUTOFF_REASON_CODES: Final = frozenset(
    {
        "standalone",
        "builder_without_coverage",
        "message_without_observation",
        "unknown_unauthenticated_traffic",
    }
)
# Every ``CreditEnforcementRejected`` message of ``credit_rollout`` (pinned by
# the same suite), as a closed reason.
REJECTION_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "credit_enforcement_at cannot be backdated before the database clock": (
            "clock_refused"
        ),
        "credit_enforcement_at cannot be future-dated past the database clock": (
            "clock_refused"
        ),
        # argparse already refuses a malformed --report-digest; the service
        # refuses it again at its own boundary.
        "expected_report_digest is not a lowercase SHA-256": (
            "report_digest_malformed"
        ),
        "credit orphan reconciliation is incomplete": "orphans_open",
        "credit readiness is incomplete": "readiness_incomplete",
        "tenant cutoff report differs from the confirmed digest": (
            "report_digest_mismatch"
        ),
        "tenant cutoff report is unaccepted": "cutoff_unaccepted",
        "credit enforcement is already armed": "already_armed",
        "cutoff acceptance author is not an active super_admin": (
            "author_not_active_super_admin"
        ),
    }
)
REASONS: Final = frozenset(
    {
        "usage",
        "import_failed",
        "settings_invalid",
        "credit_switch_off",
        "task_identity_unavailable",
        "report_digest_mismatch",
        "report_digest_malformed",
        "finding_not_in_report",
        "author_not_active_super_admin",
        "orphans_open",
        "readiness_incomplete",
        "cutoff_unaccepted",
        "clock_refused",
        "already_armed",
        "refused_unclassified",
        "readback_mismatch",
        "unexpected",
    }
)
RATIONALE_MAX: Final = 1000
ECS_METADATA_ENV: Final = "ECS_CONTAINER_METADATA_URI_V4"
ECS_METADATA_HOST: Final = "169.254.170.2"
METADATA_TIMEOUT_SECONDS: Final = 5.0
# ``credit_enforcement_state.set_by`` is VARCHAR(255).
SET_BY_MAX: Final = 255
_TASK_ARN: Final = re.compile(
    r"arn:aws(?:-[a-z]+)*:ecs:[a-z]{2}(?:-[a-z]+)+-[0-9]:[0-9]{12}:"
    r"task/[A-Za-z0-9_-]{1,255}/[0-9a-f]{32}"
)
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_EMAIL_LIKE: Final = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_READ_ONLY_SQL: Final = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"

TaskIdentity = Callable[[], Awaitable[str | None]]
SessionFactory = Callable[[], "AsyncSession"]
Finding = tuple[UUID, UUID | None, str]


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
    task_arn: str | None = None
    database_now: str | None = None
    report_digest: str | None = None
    enforcement: Mapping[str, object] | None = None
    readiness: Mapping[str, object] | None = None
    cutoff: Mapping[str, object] | None = None
    acceptance: Mapping[str, object] | None = None
    rationale: str | None = None

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
            "task_arn": self.task_arn,
            "database_now": self.database_now,
            "report_digest": self.report_digest,
            "enforcement": None if self.enforcement is None else dict(self.enforcement),
            "readiness": None if self.readiness is None else dict(self.readiness),
            "cutoff": None if self.cutoff is None else dict(self.cutoff),
            "acceptance": None if self.acceptance is None else dict(self.acceptance),
            "rationale": self.rationale,
        }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _rejection(error: BaseException) -> str:
    return REJECTION_REASONS.get(str(error), "refused_unclassified")


# ---------------------------------------------------------------------------
# This task's identity (ECS task metadata v4)
# ---------------------------------------------------------------------------


async def ecs_task_arn(
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """This ECS task's ARN, or ``None`` when it cannot be established.

    Only the link-local metadata endpoint the ECS agent injects is read
    (plain HTTP to ``169.254.170.2``, no proxy, no redirect); any other
    value, a failed read or a value that is not a full task ARN is ``None``.
    """

    source = os.environ if environ is None else environ
    base = source.get(ECS_METADATA_ENV, "").strip()
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "http"
        or parts.hostname != ECS_METADATA_HOST
        or port not in (None, 80)
        or parts.username is not None
        or parts.query
        or parts.fragment
    ):
        return None
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=METADATA_TIMEOUT_SECONDS,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = await client.get(base.rstrip("/") + "/task")
        if response.status_code != 200:
            return None
        body = response.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        # ``InvalidURL`` (a tab or CR/LF inside the variable, which
        # ``urlsplit`` silently drops) is neither of the other two.
        return None
    arn = body.get("TaskARN") if isinstance(body, dict) else None
    if not isinstance(arn, str) or len(arn) > SET_BY_MAX:
        return None
    return arn if _TASK_ARN.fullmatch(arn) else None


# ---------------------------------------------------------------------------
# Database reads shared by the subcommands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Enforcement:
    at: datetime | None
    set_by: str | None
    set_at: datetime | None

    @property
    def armed(self) -> bool:
        return self.at is not None

    def document(self) -> dict[str, object]:
        return {
            "armed": self.armed,
            "credit_enforcement_at": _iso(self.at),
            "set_by": self.set_by,
            "set_at": _iso(self.set_at),
        }


async def _read_only(session: AsyncSession) -> None:
    """The first statement of the transaction: nothing after it can write."""

    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text(_READ_ONLY_SQL))


async def _enforcement(session: AsyncSession) -> _Enforcement:
    row = (
        await session.execute(
            select(
                CreditEnforcementState.credit_enforcement_at,
                CreditEnforcementState.set_by,
                CreditEnforcementState.set_at,
            ).where(CreditEnforcementState.id == ENFORCEMENT_STATE_ID)
        )
    ).one_or_none()
    if row is None:
        return _Enforcement(None, None, None)
    return _Enforcement(row[0], row[1], row[2])


async def _fresh_enforcement(sessions: SessionFactory) -> _Enforcement:
    """The committed row, read in a session of its own."""

    async with sessions() as session:
        try:
            await _read_only(session)
            return await _enforcement(session)
        finally:
            await session.rollback()


async def _accepted_findings(session: AsyncSession, digest: str) -> set[Finding]:
    rows = (
        await session.execute(
            select(
                CreditCutoffAcceptance.tenant_id,
                CreditCutoffAcceptance.builder_id,
                CreditCutoffAcceptance.reason,
            ).where(CreditCutoffAcceptance.report_digest == digest)
        )
    ).all()
    return {(row[0], row[1], row[2]) for row in rows}


def _findings(report: CutoffReport) -> list[Finding]:
    return [(f.tenant_id, f.builder_id, f.reason) for f in report.findings]


def _cutoff_document(
    report: CutoffReport, accepted: set[Finding], *, with_findings: bool
) -> dict[str, object]:
    findings = _findings(report)
    return {
        "digest": report.digest,
        "ready": report.ready,
        "finding_count": len(findings),
        "accepted_count": sum(1 for finding in findings if finding in accepted),
        "findings": (
            [
                {
                    "tenant_id": str(tenant_id),
                    "builder_id": None if builder_id is None else str(builder_id),
                    "reason": reason,
                    "accepted": (tenant_id, builder_id, reason) in accepted,
                }
                for tenant_id, builder_id, reason in findings
            ]
            if with_findings
            else None
        ),
    }


def _orphans_document(report: OrphanReport) -> dict[str, object]:
    return {
        "open": report.open_count,
        "decision_without_effect": report.decision_without_effect,
        "effect_without_decision": report.effect_without_decision,
        "trace_partial_debit": report.trace_partial_debit,
        "orphan_outbox": report.orphan_outbox,
        "handoff_without_effect": report.handoff_without_effect,
    }


def _switch_document(reading: CreditSwitchReading) -> dict[str, object]:
    return {
        "effective": reading.effective,
        "reason": reading.reason,
        "revision": reading.revision,
    }


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


async def _status(sessions: SessionFactory, switch: dict[str, object]) -> RunResult:
    async with sessions() as session:
        try:
            await _read_only(session)
            now = await CreditRepository(session).database_now()
            enforcement = await _enforcement(session)
            service = CreditRolloutService(session)
            readiness = await service.capability_ready(now=now)
            orphans = await service.orphan_report()
            preview = await service.deletion_closure_preview(now=now)
            report = await service.cutoff_report(now=now)
            accepted = await _accepted_findings(session, report.digest)
        finally:
            await session.rollback()
    return RunResult(
        command="status",
        verdict="ok",
        switch=switch,
        database_now=_iso(now),
        enforcement=enforcement.document(),
        readiness={
            "ready": readiness.ready,
            "reconciliation_complete": readiness.reconciliation_complete,
            "readback_complete": readiness.readback_complete,
            "has_open_gap": readiness.has_open_gap,
            "has_backlog": readiness.has_backlog,
            "has_alert": readiness.has_alert,
            "open_orphans": orphans.open_count,
            "orphans": _orphans_document(orphans),
            # ``arm`` first closes the effects of deleted tenants, in its own
            # transaction, and then evaluates readiness: what it would see.
            "deletion_closures_arm_writes": preview.closures_to_write,
            "orphans_after_arm_closures": _orphans_document(preview.orphans_after),
            "ready_after_arm_closures": preview.readiness_after.ready,
        },
        cutoff=_cutoff_document(report, accepted, with_findings=False),
    )


async def _report(sessions: SessionFactory, switch: dict[str, object]) -> RunResult:
    async with sessions() as session:
        try:
            await _read_only(session)
            now = await CreditRepository(session).database_now()
            report = await CreditRolloutService(session).cutoff_report(now=now)
            accepted = await _accepted_findings(session, report.digest)
        finally:
            await session.rollback()
    return RunResult(
        command="report",
        verdict="ok",
        switch=switch,
        database_now=_iso(now),
        cutoff=_cutoff_document(report, accepted, with_findings=True),
    )


async def _read_acceptance(
    sessions: SessionFactory, acceptance_id: UUID
) -> dict[str, Any] | None:
    async with sessions() as session:
        try:
            await _read_only(session)
            row = (
                await session.execute(
                    select(
                        CreditCutoffAcceptance.id,
                        CreditCutoffAcceptance.tenant_id,
                        CreditCutoffAcceptance.builder_id,
                        CreditCutoffAcceptance.reason,
                        CreditCutoffAcceptance.report_digest,
                        CreditCutoffAcceptance.author_user_id,
                        CreditCutoffAcceptance.accepted_at,
                        CreditCutoffAcceptance.accepted_on,
                    ).where(CreditCutoffAcceptance.id == acceptance_id)
                )
            ).one_or_none()
        finally:
            await session.rollback()
    return None if row is None else dict(row._mapping)


def _acceptance_document(row: Mapping[str, Any]) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "builder_id": None if row["builder_id"] is None else str(row["builder_id"]),
        "reason": row["reason"],
        "report_digest": row["report_digest"],
        "author_user_id": str(row["author_user_id"]),
        "accepted_at": _iso(row["accepted_at"]),
        "accepted_on": row["accepted_on"].isoformat(),
    }


async def _accept(
    args: argparse.Namespace,
    sessions: SessionFactory,
    identity: TaskIdentity,
    switch: dict[str, object],
) -> RunResult:
    def result(verdict: str, *reasons: str, **fields: Any) -> RunResult:
        return RunResult(
            command="accept",
            verdict=verdict,
            reasons=reasons,
            switch=switch,
            report_digest=args.report_digest,
            rationale=args.rationale,
            **fields,
        )

    task_arn = await identity()
    if task_arn is None:
        return result("unconfigured", "task_identity_unavailable")
    armed = await _fresh_enforcement(sessions)
    if armed.armed:
        return result(
            "already_armed",
            "already_armed",
            task_arn=task_arn,
            enforcement=armed.document(),
        )
    finding: Finding = (args.tenant_id, args.builder_id, args.reason_code)
    async with sessions() as session:
        try:
            repository = CreditRepository(session)
            # The rollout lock ``arm_enforcement`` takes: an arm either
            # committed before this point (seen below) or waits for this
            # commit, so no acceptance is ever written once armed.
            await repository.lock_rollout_control(environment=settings.environment)
            if await repository.enforcement_at() is not None:
                armed_now = await _enforcement(session)
                await session.rollback()
                return result(
                    "already_armed",
                    "already_armed",
                    task_arn=task_arn,
                    enforcement=armed_now.document(),
                )
            now = await repository.database_now()
            service = CreditRolloutService(session)
            report = await service.cutoff_report(now=now)
            summary = _cutoff_document(
                report,
                await _accepted_findings(session, report.digest),
                with_findings=False,
            )
            if report.digest != args.report_digest:
                await session.rollback()
                return result(
                    "refused",
                    "report_digest_mismatch",
                    task_arn=task_arn,
                    database_now=_iso(now),
                    cutoff=summary,
                )
            if finding not in _findings(report):
                await session.rollback()
                return result(
                    "refused",
                    "finding_not_in_report",
                    task_arn=task_arn,
                    database_now=_iso(now),
                    cutoff=summary,
                )
            acceptance = await service.accept_cutoff(
                tenant_id=args.tenant_id,
                builder_id=args.builder_id,
                reason=args.reason_code,
                report_digest=args.report_digest,
                author_user_id=args.author_user_id,
            )
            acceptance_id = acceptance.id
            await session.commit()
        except CreditEnforcementRejected as error:
            await session.rollback()
            return result("refused", _rejection(error), task_arn=task_arn)
        except BaseException:
            await session.rollback()
            raise
    row = await _read_acceptance(sessions, acceptance_id)
    if row is None:
        return result("readback_mismatch", "readback_mismatch", task_arn=task_arn)
    document = _acceptance_document(row)
    if (
        row["tenant_id"],
        row["builder_id"],
        row["reason"],
        row["report_digest"],
        row["author_user_id"],
    ) != (*finding, args.report_digest, args.author_user_id):
        return result(
            "readback_mismatch",
            "readback_mismatch",
            task_arn=task_arn,
            acceptance=document,
        )
    return result(
        "accepted",
        task_arn=task_arn,
        database_now=_iso(row["accepted_at"]),
        acceptance=document,
    )


async def _arm(
    args: argparse.Namespace,
    sessions: SessionFactory,
    identity: TaskIdentity,
    switch: dict[str, object],
) -> RunResult:
    def result(verdict: str, *reasons: str, **fields: Any) -> RunResult:
        return RunResult(
            command="arm",
            verdict=verdict,
            reasons=reasons,
            switch=switch,
            report_digest=args.report_digest,
            **fields,
        )

    task_arn = await identity()
    if task_arn is None:
        return result("unconfigured", "task_identity_unavailable")
    before = await _fresh_enforcement(sessions)
    if before.armed:
        return result(
            "already_armed",
            "already_armed",
            task_arn=task_arn,
            enforcement=before.document(),
        )
    async with sessions() as session:
        try:
            state = await CreditRolloutService(session).arm_enforcement(
                set_by=task_arn,
                at=None,
                expected_report_digest=args.report_digest,
            )
            written = _Enforcement(
                state.credit_enforcement_at, state.set_by, state.set_at
            )
            await session.commit()
        except CreditEnforcementRejected as error:
            await session.rollback()
            after = await _fresh_enforcement(sessions)
            if after.armed:
                return result(
                    "already_armed",
                    "already_armed",
                    task_arn=task_arn,
                    enforcement=after.document(),
                )
            return result("refused", _rejection(error), task_arn=task_arn)
        except BaseException:
            await session.rollback()
            raise
    after = await _fresh_enforcement(sessions)
    if not after.armed or after != written or after.set_by != task_arn:
        return result(
            "readback_mismatch",
            "readback_mismatch",
            task_arn=task_arn,
            enforcement=after.document(),
        )
    return result(
        "armed",
        task_arn=task_arn,
        database_now=_iso(after.set_at),
        enforcement=after.document(),
    )


async def run(
    args: argparse.Namespace,
    *,
    session_factory: SessionFactory | None = None,
    task_identity: TaskIdentity | None = None,
) -> RunResult:
    """Run one parsed command; owns the process pool unless one is injected."""

    owned = session_factory is None
    sessions = session_factory
    identity = ecs_task_arn if task_identity is None else task_identity
    try:
        if owned and settings.credit_hot_path_enabled is True:
            from src.repositories.database import get_db_session_factory, init_database

            await init_database()
            sessions = get_db_session_factory()
        # (a) Re-read the switch here: the verdict is per process and nothing
        # else refreshes it in a one-off task.  Off, it reads nothing.
        reading = await refresh_credit_switch(session_factory=sessions)
        switch = _switch_document(reading)
        if not (reading.effective and credit_switch_effective()):
            return RunResult(
                command=args.command,
                verdict="switch_off",
                reasons=("credit_switch_off",),
                switch=switch,
            )
        if sessions is None:  # effective implies a database was read
            raise RuntimeError("no session factory for an effective switch")
        if args.command == "status":
            return await _status(sessions, switch)
        if args.command == "report":
            return await _report(sessions, switch)
        if args.command == "accept":
            return await _accept(args, sessions, identity, switch)
        return await _arm(args, sessions, identity, switch)
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


def _digest(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise argparse.ArgumentTypeError("digest")
    return value


def _uuid(value: str) -> UUID:
    """Only the canonical lowercase hyphenated form (what the report prints)."""

    try:
        parsed = UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("uuid") from error
    if str(parsed) != value:
        raise argparse.ArgumentTypeError("uuid")
    return parsed


def _builder(value: str) -> UUID | None:
    return None if value == "none" else _uuid(value)


def _rationale(value: str) -> str:
    """One printable line; no e-mail address (the rationale is printed to the
    task log and copied into a record, so it carries no personal data)."""

    stripped = value.strip()
    if (
        not stripped
        or len(stripped) > RATIONALE_MAX
        or not stripped.isprintable()
        or _EMAIL_LIKE.search(stripped)
    ):
        raise argparse.ArgumentTypeError("rationale")
    return stripped


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    """Parse one invocation; anything malformed raises ``_UsageError``."""

    # No ``--help``: the only exit 0 is a command's success verdict.
    parser = _Parser(
        prog="python -m src.ops.credit_enforcement", add_help=False, allow_abbrev=False
    )
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    commands.add_parser("status", add_help=False, allow_abbrev=False)
    commands.add_parser("report", add_help=False, allow_abbrev=False)
    accept = commands.add_parser("accept", add_help=False, allow_abbrev=False)
    accept.add_argument("--report-digest", type=_digest, required=True)
    accept.add_argument("--tenant-id", type=_uuid, required=True)
    accept.add_argument("--builder-id", type=_builder, required=True)
    accept.add_argument(
        "--reason-code", choices=sorted(CUTOFF_REASON_CODES), required=True
    )
    accept.add_argument("--author-user-id", type=_uuid, required=True)
    accept.add_argument("--rationale", type=_rationale, required=True)
    arm = commands.add_parser("arm", add_help=False, allow_abbrev=False)
    arm.add_argument("--report-digest", type=_digest, required=True)
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
    task_identity: TaskIdentity | None = None,
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
                result = await run(
                    args, session_factory=session_factory, task_identity=task_identity
                )
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

    The API's ``configure_logging`` is not used: with ``LOG_FORMAT=console``
    it renders exceptions through structlog's console renderer, which prints
    a traceback's local variables when ``rich`` is importable -- among them
    the database URL with its password (``init_database`` logs its failure
    with ``exc_info``).  ``format_exc_info`` renders the plain traceback:
    file, line and source, no locals.
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
