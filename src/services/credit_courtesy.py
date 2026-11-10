"""Courtesy credits granted and removed by a super admin (creditos-cortesia).

A courtesy is capacity, not money: a ``courtesy`` lot of the credit ledger
(:meth:`src.services.credit_ledger.LedgerBook.grant_courtesy`), consumed,
expired and settling debt like any lot, and taken back, in part or whole, by
``courtesy_removal`` entries (:meth:`LedgerBook.remove_courtesy`): funding
removed, never usage and never debt.  Nothing here reads or writes a
``billing_*`` table, a fact, an outbox row or an ack.

Both operations are ledger writes like any other: the book is opened with
:meth:`CreditLedger.open_book` -- the account lock, then the lots, the lock
order of :mod:`src.repositories.credit` -- so the clock rule, the deadline
sweep, the start catch-up of a monthly allowance and, after the rows are
inserted, the exhaustion lifecycle run exactly as for a debit or a fact.
They run in the CALLER's transaction and never commit: a refusal raises (a
:class:`CourtesyRefused` with its ``code``, or ``LedgerClockAhead``, which
the route answers ``503 credit_clock_ahead``, DESIGN Decision 16) and the
caller rolls back, so a refused request leaves nothing behind -- not even the
account row the lock step may have created.

Idempotency (DESIGN Decision 10).  Every grant and removal carries a
client ``Idempotency-Key`` (1 to 200 characters), unique across every
builder in ``credit_courtesy_events``, and the SHA-256 of the canonical JSON
of the request (:func:`courtesy_request_digest`: the operation, the builder,
the lot of a removal and the whole body).  The key is looked up before any
lock and again under the account lock (a same-builder writer of the same
key committed while this one waited): the same digest answers the recorded
result (``replayed=True``) with no write; another digest is
:class:`CourtesyIdempotencyKeyReused`.  A writer of another builder holds
another lock, so the two can race to the unique key: the ledger rows and the
event are inserted in one SAVEPOINT, the event ``ON CONFLICT DO NOTHING``;
the loser rolls its savepoint back (no lot, no entry, no lifecycle row of
its own survives), re-reads the winner's event and answers by the same two
rules -- never an unhandled error.

Builder (DESIGN Decision 14).  A grant needs an existing, active builder
(``is_active`` and ``status = 'active'``); a removal is maintenance and is
allowed for an inactive one.  A removal's builder is the lot's own: a
``lot_id`` that does not exist or is not a courtesy lot is
:class:`CourtesyLotNotFound`, the same refusal for every case (Decision 6).

Logs (DESIGN Decision 13) name the builder, the lot, the amount, the actor
and the event, never the reason.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.models.credit import (
    COURTESY_IDEMPOTENCY_KEY_MAX_LENGTH,
    COURTESY_REASON_MAX_LENGTH,
)
from src.repositories.credit import CourtesyEventRow, CreditRepository
from src.services.credit_ledger import (
    MILLICREDITS_PER_CREDIT,
    CourtesyLotNotFound,
    CourtesyRefused,
    CourtesyRemovalExceedsBalance,
    CreditLedger,
    LedgerBook,
    LedgerClockAhead,
    LedgerInvariantError,
)

logger = get_logger(__name__)

# DESIGN Decision 3: whole credits, 1 to 100,000 per grant.
COURTESY_MAX_CREDITS: Final = 100_000
GRANT_OPERATION: Final = "grant"
REMOVAL_OPERATION: Final = "removal"
CourtesyOperation = Literal["grant", "removal"]


class CourtesyBuilderNotFound(CourtesyRefused):
    """A grant for a builder that does not exist."""

    code = "builder_not_found"


class CourtesyBuilderInactive(CourtesyRefused):
    """A grant for an inactive builder (DESIGN Decision 14)."""

    code = "builder_inactive"


class CourtesyIdempotencyKeyReused(CourtesyRefused):
    """The key was already used for another request (DESIGN Decision 10)."""

    code = "idempotency_key_reused"


class _KeyLost(Exception):
    """Internal: a concurrent writer committed the same key first."""


@dataclass(frozen=True)
class CourtesyGrantResult:
    """A grant as recorded (``replayed``: the same request again)."""

    event_id: UUID
    builder_id: UUID
    lot_id: UUID
    granted_mc: int
    expires_at: datetime
    occurred_at: datetime
    actor_user_id: UUID
    replayed: bool = False


@dataclass(frozen=True)
class CourtesyRemovalResult:
    """A removal as recorded (``replayed``: the same request again)."""

    event_id: UUID
    builder_id: UUID
    lot_id: UUID
    removed_mc: int
    occurred_at: datetime
    actor_user_id: UUID
    replayed: bool = False


# ---------------------------------------------------------------------------
# Pure rules.
# ---------------------------------------------------------------------------


def courtesy_request_digest(
    operation: CourtesyOperation,
    *,
    builder_id: UUID,
    lot_id: UUID | None,
    body: Mapping[str, Any],
) -> str:
    """SHA-256 of the canonical JSON of a request (DESIGN Decision 10).

    The operation, the builder, the lot (a removal's; ``None`` for a grant)
    and the whole body, with sorted keys and no whitespace, so the same
    request always has the same digest and a key reused on another lot,
    builder or body never does."""

    if operation not in (GRANT_OPERATION, REMOVAL_OPERATION):
        raise ValueError("unknown courtesy operation")
    canonical = json.dumps(
        {
            "operation": operation,
            "builder_id": str(builder_id),
            "lot_id": None if lot_id is None else str(lot_id),
            "body": dict(body),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def grant_body(builder_id: UUID, credits: int, reason: str) -> dict[str, Any]:
    """The body of ``POST /courtesy-grants`` as the digest reads it."""

    return {"builder_id": str(builder_id), "credits": credits, "reason": reason}


def removal_body(
    *, credits: int | None, remove_all: bool, reason: str
) -> dict[str, Any]:
    """The body of ``POST /courtesy-grants/{lot_id}/removals``: exactly one
    of ``credits`` and ``remove_all``, as the client sent it."""

    if remove_all:
        return {"remove_all": True, "reason": reason}
    return {"credits": credits, "reason": reason}


def _check_credits(credits: object, *, maximum: int | None) -> int:
    if type(credits) is not int or credits < 1:
        raise ValueError("credits must be a positive whole number")
    if maximum is not None and credits > maximum:
        raise ValueError(f"credits must not exceed {maximum}")
    return credits


def _check_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must have 1 to {maximum} characters")
    if "\x00" in value:
        # PostgreSQL text cannot hold NUL: refused here, never a 500.
        raise ValueError(f"{name} must not contain NUL characters")
    return value


def _bound_ledger(session: AsyncSession, ledger: CreditLedger | None) -> CreditLedger:
    """The injected ledger, which must write through ``session`` (the caller's
    transaction), or a new one bound to it."""

    if ledger is None:
        return CreditLedger(session)
    if ledger.session is not session:
        raise ValueError("ledger must be bound to the given session")
    return ledger


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------


async def _recorded(
    repository: CreditRepository, idempotency_key: str, digest: str
) -> CourtesyEventRow | None:
    """The event already written for the key and this very request, or
    ``None`` when the key is free; a key written for anything else is
    :class:`CourtesyIdempotencyKeyReused`."""

    event = await repository.courtesy_event_by_key(idempotency_key)
    if event is None:
        return None
    if event.request_digest != digest:
        logger.warning(
            "credit courtesy refused: idempotency key reused",
            builder_id=str(event.builder_id),
            lot_id=str(event.lot_id),
            event_id=str(event.id),
        )
        raise CourtesyIdempotencyKeyReused(
            "the idempotency key was used for another request"
        )
    return event


async def _grant_replay(
    repository: CreditRepository, event: CourtesyEventRow
) -> CourtesyGrantResult:
    lot = await repository.lot_identity(event.lot_id)
    if lot is None or event.event_kind != GRANT_OPERATION:
        raise LedgerInvariantError("a recorded courtesy grant names no courtesy lot")
    return CourtesyGrantResult(
        event_id=event.id,
        builder_id=event.builder_id,
        lot_id=event.lot_id,
        granted_mc=event.amount_mc,
        expires_at=lot.expires_at,
        occurred_at=event.occurred_at,
        actor_user_id=event.actor_user_id,
        replayed=True,
    )


def _removal_replay(event: CourtesyEventRow) -> CourtesyRemovalResult:
    if event.event_kind != REMOVAL_OPERATION:
        raise LedgerInvariantError("a recorded removal is not a removal")
    return CourtesyRemovalResult(
        event_id=event.id,
        builder_id=event.builder_id,
        lot_id=event.lot_id,
        removed_mc=event.amount_mc,
        occurred_at=event.occurred_at,
        actor_user_id=event.actor_user_id,
        replayed=True,
    )


async def _write(
    session: AsyncSession,
    ledger: CreditLedger,
    book: LedgerBook,
    event: Mapping[str, Any],
) -> bool:
    """Persist the book and insert the event in one savepoint; ``False``
    (savepoint rolled back, nothing of this request left) when a concurrent
    writer committed the same idempotency key first."""

    repository = ledger.repository

    async def insert_event() -> None:
        if not await repository.insert_courtesy_event(**event):
            raise _KeyLost()

    try:
        async with session.begin_nested():
            await ledger.persist(book, before_lifecycle=insert_event)
    except _KeyLost:
        return False
    return True


# ---------------------------------------------------------------------------
# The two operations.
# ---------------------------------------------------------------------------


async def grant(
    session: AsyncSession,
    *,
    builder_id: UUID,
    credits: int,
    reason: str,
    actor_user_id: UUID,
    idempotency_key: str,
    at: datetime | None = None,
    ledger: CreditLedger | None = None,
) -> CourtesyGrantResult:
    """Grant ``credits`` courtesy credits to ``builder_id`` (caller's
    transaction; see the module docstring for refusals and replays).

    Raises :class:`CourtesyBuilderNotFound`, :class:`CourtesyBuilderInactive`,
    :class:`CourtesyIdempotencyKeyReused` or ``LedgerClockAhead`` (nothing
    written); ``ValueError`` for an argument the route's schema already
    refuses (credits outside 1..100,000, reason or key outside their
    lengths or holding a NUL) or a ``ledger`` bound to another session."""

    _check_credits(credits, maximum=COURTESY_MAX_CREDITS)
    _check_text(reason, "reason", COURTESY_REASON_MAX_LENGTH)
    _check_text(idempotency_key, "idempotency_key", COURTESY_IDEMPOTENCY_KEY_MAX_LENGTH)
    ledger = _bound_ledger(session, ledger)
    repository = ledger.repository
    digest = courtesy_request_digest(
        GRANT_OPERATION,
        builder_id=builder_id,
        lot_id=None,
        body=grant_body(builder_id, credits, reason),
    )
    recorded = await _recorded(repository, idempotency_key, digest)
    if recorded is not None:
        return await _grant_replay(repository, recorded)
    active = await repository.builder_is_active(builder_id)
    if active is None:
        raise CourtesyBuilderNotFound("the builder does not exist")
    if not active:
        logger.warning(
            "credit courtesy grant refused: builder inactive",
            builder_id=str(builder_id),
            actor_user_id=str(actor_user_id),
        )
        raise CourtesyBuilderInactive("the builder is not active")

    instant = at or _now()
    book = await ledger.open_book(builder_id, at=instant)
    recorded = await _recorded(repository, idempotency_key, digest)
    if recorded is not None:
        return await _grant_replay(repository, recorded)
    event_id = uuid4()
    granted_mc = credits * MILLICREDITS_PER_CREDIT
    lot = book.grant_courtesy(event_id, granted_mc, at=instant)
    occurred_at = book.clock
    assert occurred_at is not None
    written = await _write(
        session,
        ledger,
        book,
        {
            "event_id": event_id,
            "builder_id": builder_id,
            "lot_id": lot.id,
            "event_kind": GRANT_OPERATION,
            "amount_mc": granted_mc,
            "reason": reason,
            "actor_user_id": actor_user_id,
            "idempotency_key": idempotency_key,
            "request_digest": digest,
            "occurred_at": occurred_at,
        },
    )
    if not written:
        recorded = await _recorded(repository, idempotency_key, digest)
        if recorded is None:
            raise LedgerInvariantError("a lost idempotency key has no event")
        return await _grant_replay(repository, recorded)
    logger.info(
        "credit courtesy granted",
        builder_id=str(builder_id),
        lot_id=str(lot.id),
        event_id=str(event_id),
        amount_mc=granted_mc,
        actor_user_id=str(actor_user_id),
    )
    return CourtesyGrantResult(
        event_id=event_id,
        builder_id=builder_id,
        lot_id=lot.id,
        granted_mc=granted_mc,
        expires_at=lot.expires_at,
        occurred_at=occurred_at,
        actor_user_id=actor_user_id,
    )


async def remove(
    session: AsyncSession,
    *,
    lot_id: UUID,
    reason: str,
    actor_user_id: UUID,
    idempotency_key: str,
    credits: int | None = None,
    remove_all: bool = False,
    at: datetime | None = None,
    ledger: CreditLedger | None = None,
) -> CourtesyRemovalResult:
    """Remove ``credits`` whole credits, or with ``remove_all`` the whole
    spendable balance, from the courtesy lot ``lot_id`` (caller's
    transaction; see the module docstring for refusals and replays).

    Raises :class:`CourtesyLotNotFound`,
    :class:`CourtesyRemovalExceedsBalance`,
    :class:`CourtesyIdempotencyKeyReused` or ``LedgerClockAhead`` (nothing
    written); ``ValueError`` for an argument the route's schema already
    refuses (both or neither of ``credits`` and ``remove_all``, a
    non-positive ``credits``, reason or key outside their lengths or holding
    a NUL) or a ``ledger`` bound to another session."""

    if remove_all is not True and remove_all is not False:
        raise ValueError("remove_all must be a boolean")
    if remove_all == (credits is not None):
        raise ValueError("give exactly one of credits and remove_all")
    if credits is not None:
        _check_credits(credits, maximum=None)
    _check_text(reason, "reason", COURTESY_REASON_MAX_LENGTH)
    _check_text(idempotency_key, "idempotency_key", COURTESY_IDEMPOTENCY_KEY_MAX_LENGTH)
    ledger = _bound_ledger(session, ledger)
    repository = ledger.repository
    identity = await repository.lot_identity(lot_id)
    if identity is None or identity.lot_kind != "courtesy":
        raise CourtesyLotNotFound("no such courtesy lot")
    builder_id = identity.builder_id
    digest = courtesy_request_digest(
        REMOVAL_OPERATION,
        builder_id=builder_id,
        lot_id=lot_id,
        body=removal_body(credits=credits, remove_all=remove_all, reason=reason),
    )
    recorded = await _recorded(repository, idempotency_key, digest)
    if recorded is not None:
        return _removal_replay(recorded)

    instant = at or _now()
    book = await ledger.open_book(builder_id, at=instant)
    recorded = await _recorded(repository, idempotency_key, digest)
    if recorded is not None:
        return _removal_replay(recorded)
    event_id = uuid4()
    try:
        removed_mc = book.remove_courtesy(
            lot_id,
            event_id,
            credits=credits,
            remove_all=remove_all,
            at=instant,
        )
    except CourtesyRemovalExceedsBalance as refusal:
        logger.warning(
            "credit courtesy removal refused: exceeds the lot's balance",
            builder_id=str(builder_id),
            lot_id=str(lot_id),
            requested_mc=refusal.requested_mc,
            spendable_mc=refusal.spendable_mc,
            actor_user_id=str(actor_user_id),
        )
        raise
    occurred_at = book.clock
    assert occurred_at is not None
    written = await _write(
        session,
        ledger,
        book,
        {
            "event_id": event_id,
            "builder_id": builder_id,
            "lot_id": lot_id,
            "event_kind": REMOVAL_OPERATION,
            "amount_mc": removed_mc,
            "reason": reason,
            "actor_user_id": actor_user_id,
            "idempotency_key": idempotency_key,
            "request_digest": digest,
            "occurred_at": occurred_at,
        },
    )
    if not written:
        recorded = await _recorded(repository, idempotency_key, digest)
        if recorded is None:
            raise LedgerInvariantError("a lost idempotency key has no event")
        return _removal_replay(recorded)
    logger.info(
        "credit courtesy removed",
        builder_id=str(builder_id),
        lot_id=str(lot_id),
        event_id=str(event_id),
        amount_mc=removed_mc,
        remove_all=remove_all,
        actor_user_id=str(actor_user_id),
    )
    return CourtesyRemovalResult(
        event_id=event_id,
        builder_id=builder_id,
        lot_id=lot_id,
        removed_mc=removed_mc,
        occurred_at=occurred_at,
        actor_user_id=actor_user_id,
    )


__all__ = [
    "COURTESY_MAX_CREDITS",
    "GRANT_OPERATION",
    "REMOVAL_OPERATION",
    "CourtesyBuilderInactive",
    "CourtesyBuilderNotFound",
    "CourtesyGrantResult",
    "CourtesyIdempotencyKeyReused",
    "CourtesyLotNotFound",
    "CourtesyRefused",
    "CourtesyRemovalExceedsBalance",
    "CourtesyRemovalResult",
    "LedgerClockAhead",
    "courtesy_request_digest",
    "grant",
    "grant_body",
    "remove",
    "removal_body",
]
