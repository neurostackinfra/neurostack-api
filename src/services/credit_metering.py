"""Observational metering (neurostack-credit-system Group 3a).

With the credit switch effective and before ``credit_enforcement_at``, every
governed decision of the allowlist leaves one ingress claim (when it has an
ingress id) and one row of ``credit_observational_effects`` -- what WOULD be
debited -- without changing the response, its status, the stored events or
the latency beyond one bounded budget.  Nothing here debits, blocks or
answers ``credit_pending``: that is Groups 3b-1/3b-2, after the enforcement.

Inputs come from ``main`` as it is:

* the ORIGIN of a decision is ``context.origin`` of the single
  :class:`~src.interaction_context.ResolvedInteractionContext` the ingress
  built (never ``events.interaction_origin`` as stored, never a second
  classifier): API key and Meta (``classify_interaction_origin``) and Z-API
  (its adapter) are ``production``; the panel JWT is ``simulator``; Uazapi,
  Evolution, a JWT outside the allowlist and callers without proof are
  ``unknown``;
* ``authenticated`` and ``principal_id`` come from
  :func:`~src.services.credit_principal.derive_credit_principal` over the
  identity ``main``'s auth proved (Uazapi/Evolution: ``authenticated=False``)
  and the tenant's builder re-read in the database;
* the anchor of a decision (M3) is the ``interaction_id`` of its ingress
  claim or, without a claim, the public ``event_id`` of the response (the
  ``srm.decision`` row) -- never ``context.interaction_event_id``, which is a
  reservation, not a proof.

Ingress ids (deliverable 2): ``POST /v1/events`` of origin ``production``
claims ``(tenant_id, principal_id, Idempotency-Key)`` -- without a valid key,
and for ``simulator``/``unknown``, there is no claim (H3) --; Z-API
``body.messageId``; Meta the processed ``messages[0].id``; Uazapi
``body.message.messageid`` (falling back to ``body.message.id``); Evolution
``body.data.key.id``; providers claim ``(tenant_id, source,
provider_message_id)``.  A NEW claim takes the decision's anchor as its
``interaction_id``; on an insert conflict (a retry, a redelivery once the
replay store forgot it) the existing claim's ``interaction_id`` is reused, so
``trace:{tenant_id}:{interaction_id}:decision:1`` is written once.  Claims
are ``mode='observational'``: ``main`` keeps deciding duplicates through
``_claim_incoming_message``.

Cost (allowlist of the common rules): a decision ``would_debit_mc`` one trace
(:data:`TRACE_COST_MC`) only when its origin is ``production``, its principal
is authenticated and -- for a webhook -- it has a deliverable response (LLM,
deterministic or handoff; without one it costs zero, OWNER-e).  A
``POST /v1/events`` decision costs one trace even without an LLM (D5).  The
Meta file handoff (both call sites, O2) is a production decision of its own.

The writer (deliverable 4).  It starts only after the request's session and
the main path's sessions ended their transactions (the caller ends the
route's read transaction with :func:`end_route_transaction`), so a request
never holds two of the pool's connections.  It runs in its OWN transaction of
``get_db_session_factory()`` -- the process pool, no new pool, never a
SAVEPOINT of the caller's session -- and the whole of it (connection
checkout, every statement, the commit and the session close with its
ROLLBACK) fits ONE ``asyncio.timeout`` of
:data:`OBSERVATIONAL_WRITE_BUDGET_SECONDS`.  On PostgreSQL ``lock_timeout``
and ``statement_timeout`` are set LOCAL to the transaction as server-side
backstops only (they die with its commit or rollback).  A connection
interrupted by the budget is invalidated, never returned to the pool in the
middle of a statement.  A single-connection pool (SQLite ``:memory:``, the
test suites) is never cancelled mid-statement -- SQLAlchemy would invalidate
its only connection and the database with it --: the same deadline is
checked between the steps instead.  It never waits for the pool: with no free connection
(``pool_size`` checked out, ``max_overflow`` reached) the write is dropped
at once (``pool_exhausted``) instead of queueing for one.  It reads
``credit_enforcement_at`` once and writes nothing once it is set (logged at
info, not as an alert: simulator/unknown traffic keeps reaching the writer
after the enforcement).  Any failure -- no free connection, the budget, a
constraint, a trigger, a lock timeout, PostgreSQL unavailable -- is logged
(``credit observational write dropped``, the alert) and swallowed: events,
status and response stay ``main``'s.  Only a cancellation of the request
itself propagates.  In this group a drop is only logged (owner, 2026-10-28):
no metric, and a per-tenant drop counter is Group 7's.

With the switch off (captured once per request at entry), none of this runs.

Enforced ``POST /v1/events`` (Group 3b-1).  A request is ENFORCED only when
it captured the switch on at entry, its live origin is ``production`` with an
authenticated principal (an API key) and ``credit_enforcement_at`` -- read
ONCE, by :func:`capture_events_enforcement`, right after the reserved-field
422 and with no write -- is set; anything else keeps ``main``'s path and the
writer above (``simulator`` and ``unknown`` never claim, debit or block).
The route answers, in this order (MEDIUM-1): 403 ``caller_role_forbidden``
-> 422 reserved field -> 400 ``idempotency_key_required`` /
``idempotency_key_invalid`` -> 403 ``tenant_identity_mismatch`` (a plain
tenant-scoped read of the session; the only answer that gives the rate-limit
slot back) -> the claim's table (:func:`read_events_claim`: 200 replay, 409
``payload_conflict`` / ``duplicate_event`` / ``event_in_progress`` with a
capped ``Retry-After`` / ``event_failed``) -> 402 ``credit_pending``
(:func:`precheck_events`: standalone tenant or quarantine hold; nothing
written, no LLM, the slot stays consumed) -> the claim insert or takeover
(:func:`acquire_events_claim`, its own short transaction) -> phase 1
(:func:`run_enforced_phase1`) -> the LLM, outside every transaction -> the
final transaction (:func:`run_enforced_final`).  The claim is fenced by a
lease of :data:`CLAIM_LEASE_SECONDS` (see :mod:`src.repositories.credit`);
whoever loses a fence rolls back and answers by the table, never taking the
claim over by itself, and a known failure releases its own lease
(:func:`release_events_lease`).  One decision costs one trace
(:data:`TRACE_COST_MC`), with or without an LLM answer (D5): debited in
phase 1 with a synchronous handoff (atomic with the handoff record, O1-A),
otherwise in the final transaction together with the bundle and the stored
answer.  A hold that shows up only there writes the effect of zero
:data:`CREDIT_PENDING_EFFECT_KIND` and delivers the answer (D2).  The stored
answer is replayable for ``credit_replay_retention_hours``; afterwards (or
with a ``{}`` payload) a retry answers 409 ``duplicate_event`` (OWNER-d).
Accepted race at the enforcement boundary (LOW-16): a request captured as
observational just before ``credit_enforcement_at`` (its observational claim
is written only after its answer) and a retry of the same key captured as
enforced afterwards may both execute, the first one free of charge.

Enforced Z-API and Meta webhooks (Group 3b-2).  Each adapter runs its
pre-check (:func:`precheck_zapi` / :func:`precheck_meta`, the same as
3b-1's: ``credit_enforcement_at`` read once, standalone, account on demand,
hold) right after ``_check_pause_state`` and before the session is created;
``credit_pending`` answers 200 ``{"ok": true, "skipped": "credit_pending"}``
with no session row, no LGPD notice, no Meta file ack and no Meta STT (on
Z-API ``audio_too_long``, the STT and its fallback, and the mark-as-read run
before the pre-check and still reach the patient: accepted per channel,
D4/OWNER-c).  An enforced decision claims ``(tenant_id, source,
provider_message_id)`` in a short transaction right before phase 1
(:func:`acquire_webhook_claim`); its fences are 3b-1's without the lease
clause (no takeover, no ``event_failed``); a redelivery answers ALWAYS 200
``duplicate_message`` (``payload_conflict`` when the stable fields differ),
never a second reply.  The final transaction (:func:`run_webhook_final`)
charges one trace for a deliverable decision (LLM, deterministic answer or
handoff), writes ``trace_no_response`` (zero) otherwise, and stores the
reply in a ``pending`` outbox row; :func:`deliver_reply_inline` claims it
``pending -> sending`` before sending.  Owner decisions of the 3b-2 review:
a failed (or timed-out: uncertain) send is ``failed`` at once with its PII
erased, like main's lost reply (OD-1); every send first re-checks the
session's pause state against the one phase 1 left, the bot and a maximum
age (OD-2); only the provider call is under the send timeout, and the
bookkeeping after it is shielded and bounded (OD-5).
:class:`CreditResponsePublisher` only recovers the crash windows, at most
once per row; :func:`run_retention_sweeper` erases expired payloads config
on or off (OD-3).  The Meta file handoff is one enforced transaction
(:func:`run_meta_file_handoff`).  Uazapi and Evolution (``unknown``) never
reach any of this.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.pool import QueuePool, SingletonThreadPool, StaticPool
from starlette.requests import HTTPConnection

from src.api.schemas.events import CanonicalEventRequest
from src.config.logging import get_logger
from src.config.settings import Settings, settings
from src.interaction_context import InteractionOrigin, ResolvedInteractionContext
from src.repositories.credit import (
    ClaimRow,
    CreditRepository,
    OutboxClaim,
    ResponseOutboxRow,
)
from src.services import credit_lifecycle
from src.services.credit_ledger import MILLICREDITS_PER_CREDIT, CreditLedger
from src.services.credit_principal import (
    AuthenticatedIdentity,
    CreditPrincipal,
    TenantBuilder,
    credit_identity,
    derive_credit_principal,
    identity_from_tenant_context,
    read_tenant_builder,
    request_credit_identity,
)
from src.services.credit_switch import (
    credit_switch_effective,
    request_credit_switch,
    run_credit_worker_tick,
)

logger = get_logger(__name__)

# One trace is one credit (Decision 8); pinned by test.
TRACE_COST_MC: Final = MILLICREDITS_PER_CREDIT
# The observational writer never re-decides: its decisions are version 1.
OBSERVATIONAL_DECISION_VERSION: Final = 1
TRACE_EFFECT_KIND: Final = "trace"
OBSERVATIONAL_MODE: Final = "observational"
# The whole write: checkout, statements and commit (deliverable 4).
OBSERVATIONAL_WRITE_BUDGET_SECONDS: Final = 0.25
# Server-side backstops on PostgreSQL (SET LOCAL semantics via set_config).
OBSERVATIONAL_SERVER_TIMEOUT: Final = "250ms"
IDEMPOTENCY_KEY_HEADER: Final = "Idempotency-Key"
# ``credit_ingress_claims.idempotency_key`` / ``provider_message_id`` width.
INGRESS_ID_MAX_LENGTH: Final = 255
DECISION_DIGEST_SCHEMA: Final = "credit_decision.v1"
WRITE_DROPPED_LOG: Final = "credit observational write dropped"

IngressSource = Literal["events", "zapi", "meta", "uazapi", "evolution"]
ObservationalOutcome = Literal[
    "recorded",  # the effect row was inserted
    "replayed",  # the effect already existed (retry, redelivery)
    "enforced",  # credit_enforcement_at is set: the observational mode is over
    "tenant_gone",  # the tenant row no longer exists
    "route_busy",  # the route's transaction could not be ended: not written
    "pool_exhausted",  # no free pooled connection: dropped without waiting
    "timeout",  # the budget ran out (lock wait, slow commit, a lost race)
    "failed",  # any other error (constraint, trigger, connection lost ...)
]

_SET_SERVER_TIMEOUTS: Final = (
    "SELECT set_config('lock_timeout', :timeout, true), "
    "set_config('statement_timeout', :timeout, true)"
)


# ---------------------------------------------------------------------------
# Identities, digests and the cost rule (pure).
# ---------------------------------------------------------------------------


def canonical_json(value: object) -> bytes:
    """Sorted keys, compact separators, UTF-8 (the digests' only encoding)."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trace_effect_id(
    tenant_id: UUID,
    interaction_id: UUID,
    decision_version: int = OBSERVATIONAL_DECISION_VERSION,
) -> str:
    """``trace:{tenant_id}:{interaction_id}:decision:{decision_version}``.

    Keyed by the tenant, so it exists for a standalone tenant and survives a
    reparent; the builder is the row's ``builder_id`` column.
    """

    return f"trace:{tenant_id}:{interaction_id}:decision:{decision_version}"


def valid_ingress_id(raw: object) -> str | None:
    """A storable ingress id: 1..255 visible ASCII characters, else ``None``."""

    if not isinstance(raw, str) or not raw:
        return None
    if len(raw) > INGRESS_ID_MAX_LENGTH:
        return None
    if not all("\x21" <= character <= "\x7e" for character in raw):
        return None
    return raw


def event_payload_sha256(request: CanonicalEventRequest) -> str:
    """SHA-256 of the canonical JSON of the canonical request: effective
    tenant, session, event type and payload."""

    return sha256_hex(canonical_json(request.model_dump(mode="json")))


def _text(value: object) -> object:
    return (
        value if isinstance(value, (str, int, float, bool)) or value is None else None
    )


def zapi_payload_sha256(body: Mapping[str, Any]) -> str:
    """Only the stable fields of a Z-API message (volatile ones never count)."""

    text_obj = body.get("text")
    audio_obj = body.get("audio")
    text_obj = text_obj if isinstance(text_obj, Mapping) else {}
    audio_obj = audio_obj if isinstance(audio_obj, Mapping) else {}
    return sha256_hex(
        canonical_json(
            {
                "messageId": _text(body.get("messageId")),
                "phone": _text(body.get("phone")),
                "fromMe": _text(body.get("fromMe")),
                "text": _text(text_obj.get("message")),
                "audio_url": _text(audio_obj.get("audioUrl") or audio_obj.get("url")),
                "audio_seconds": _text(audio_obj.get("seconds")),
            }
        )
    )


def provider_message_sha256(message: Mapping[str, Any]) -> str:
    """The processed provider message object (Meta ``messages[0]``, Uazapi
    ``message``, Evolution ``data``)."""

    return sha256_hex(canonical_json(message))


def decision_digest(
    *,
    tenant_id: UUID,
    interaction_id: UUID,
    decision_version: int,
    origin: InteractionOrigin,
    decision: Mapping[str, Any],
) -> str:
    """Canonical SHA-256 of one durable decision (pinned by test)."""

    return sha256_hex(
        canonical_json(
            {
                "schema": DECISION_DIGEST_SCHEMA,
                "tenant_id": str(tenant_id),
                "interaction_id": str(interaction_id),
                "decision_version": decision_version,
                "origin": origin.value,
                "decision": dict(decision),
            }
        )
    )


def would_debit_mc(
    *, origin: InteractionOrigin, authenticated: bool, deliverable: bool
) -> int:
    """One trace for an authenticated production decision with a deliverable
    response; zero for ``simulator``, ``unknown``, ``authenticated=False``
    and a webhook decision without a deliverable response."""

    if origin is InteractionOrigin.PRODUCTION and authenticated and deliverable:
        return TRACE_COST_MC
    return 0


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def response_decision(response: object) -> tuple[UUID, dict[str, Any]] | None:
    """The public ``event_id`` and the canonical, PII-free projection of an
    ``EventResponse``; ``None`` when ``response`` is not one."""

    event_id = getattr(response, "event_id", None)
    if not isinstance(event_id, UUID):
        return None
    decision = getattr(response, "decision", None)
    prompt = getattr(response, "prompt", None)
    projection: dict[str, Any] = {
        "kind": "pipeline",
        "event_id": str(event_id),
        "mode": _text(_enum_value(getattr(response, "mode", None))),
        "handoff": getattr(response, "handoff_summary", None) is not None,
        "responded": bool(getattr(prompt, "suggested_response", None)),
        "deterministic": bool(getattr(prompt, "deterministic", False)),
    }
    for name in (
        "intent",
        "intent_category",
        "funnel_stage",
        "action",
        "safety_concern",
        "safety_category",
        "needs_rag",
        "handoff_required",
        "handoff_reason",
    ):
        projection[name] = _text(getattr(decision, name, None))
    return event_id, projection


def deliverable_response(response: object) -> bool:
    """A webhook decision is deliverable with an LLM or deterministic answer
    (``prompt.suggested_response``) or a handoff."""

    prompt = getattr(response, "prompt", None)
    return bool(getattr(prompt, "suggested_response", None)) or (
        getattr(response, "handoff_summary", None) is not None
    )


# ---------------------------------------------------------------------------
# The ingress, captured once per message (only with the switch on).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationalIngress:
    """What one ingress knows about the message it is processing."""

    source: IngressSource
    tenant_id: UUID
    session_id: UUID
    origin: InteractionOrigin
    identity: AuthenticatedIdentity | None
    payload_sha256: str
    idempotency_key: str | None = None
    provider_message_id: str | None = None


def _ingress(
    request: HTTPConnection,
    *,
    source: IngressSource,
    context: ResolvedInteractionContext,
    identity: Callable[[], AuthenticatedIdentity | None],
    payload_sha256: Callable[[], str],
    idempotency_key: Callable[[], str | None] = lambda: None,
    provider_message_id: object = None,
) -> ObservationalIngress | None:
    """``None`` (and no other work) unless the request captured the switch
    on; never raises."""

    try:
        if not request_credit_switch(request):
            return None
        return ObservationalIngress(
            source=source,
            tenant_id=context.effective_tenant_id,
            session_id=context.session_id,
            origin=context.origin,
            identity=identity(),
            payload_sha256=payload_sha256(),
            idempotency_key=idempotency_key(),
            provider_message_id=valid_ingress_id(provider_message_id),
        )
    except Exception as error:
        logger.warning(
            WRITE_DROPPED_LOG,
            alert="credit_observational_write",
            reason="ingress_unreadable",
            source=source,
            error_type=type(error).__name__,
        )
        return None


def events_ingress(
    request: HTTPConnection,
    *,
    canonical_request: CanonicalEventRequest,
    context: ResolvedInteractionContext,
) -> ObservationalIngress | None:
    """``POST /v1/events``: the identity the middleware published and the
    ``Idempotency-Key`` header (a malformed key is no key before the
    enforcement: the response stays ``main``'s)."""

    return _ingress(
        request,
        source="events",
        context=context,
        identity=lambda: request_credit_identity(request),
        payload_sha256=lambda: event_payload_sha256(canonical_request),
        idempotency_key=lambda: valid_ingress_id(
            request.headers.get(IDEMPOTENCY_KEY_HEADER)
        ),
    )


def zapi_ingress(
    request: HTTPConnection,
    *,
    body: Mapping[str, Any],
    context: ResolvedInteractionContext,
) -> ObservationalIngress | None:
    """Z-API: capability token proved, ``body.messageId``."""

    return _ingress(
        request,
        source="zapi",
        context=context,
        identity=lambda: credit_identity("zapi", context.effective_tenant_id),
        payload_sha256=lambda: zapi_payload_sha256(body),
        provider_message_id=str(body.get("messageId") or "").strip(),
    )


def meta_ingress(
    request: HTTPConnection,
    *,
    message: Mapping[str, Any],
    context: ResolvedInteractionContext,
) -> ObservationalIngress | None:
    """Meta: the HMAC identity of the middleware, ``messages[0].id``."""

    return _ingress(
        request,
        source="meta",
        context=context,
        identity=lambda: request_credit_identity(request),
        payload_sha256=lambda: provider_message_sha256(message),
        provider_message_id=str(message.get("id") or "").strip(),
    )


def uazapi_ingress(
    request: HTTPConnection,
    *,
    message: Mapping[str, Any],
    context: ResolvedInteractionContext,
) -> ObservationalIngress | None:
    """Uazapi: routed by instance name only (``authenticated=False``),
    ``message.messageid`` falling back to ``message.id``."""

    return _ingress(
        request,
        source="uazapi",
        context=context,
        identity=lambda: credit_identity("uazapi", context.effective_tenant_id),
        payload_sha256=lambda: provider_message_sha256(message),
        provider_message_id=str(message.get("messageid") or message.get("id") or ""),
    )


def evolution_ingress(
    request: HTTPConnection,
    *,
    data: Mapping[str, Any],
    context: ResolvedInteractionContext,
) -> ObservationalIngress | None:
    """Evolution: routed by instance name only (``authenticated=False``),
    ``data.key.id``."""

    key = data.get("key")
    return _ingress(
        request,
        source="evolution",
        context=context,
        identity=lambda: credit_identity("evolution", context.effective_tenant_id),
        payload_sha256=lambda: provider_message_sha256(data),
        provider_message_id=str(
            (key.get("id") if isinstance(key, Mapping) else None) or ""
        ).strip(),
    )


# ---------------------------------------------------------------------------
# The route's transaction and the writer.
# ---------------------------------------------------------------------------


async def end_route_transaction(db: object) -> bool:
    """End the route session's open transaction (a read one at this point),
    so its pooled connection goes back before the writer takes one.

    ``True`` when the session holds no transaction afterwards (or is not a
    real session); ``False`` when it could not be ended -- then the writer
    must not run.  Rolling back is what closing the request session would
    do anyway; it is never called with pending writes of the main path.

    It refuses (``False``, logged ``route_pending_writes``, nothing rolled
    back) when the session still holds ORM changes (``new``, ``dirty`` or
    ``deleted``): the rollback would discard them -- and any flushed but
    uncommitted write -- and expire every loaded object, so a metering side
    effect must never be the one deciding that.  Flushed rows are not
    visible in those collections; the caller contract (the main path
    committed before this runs) still covers them.
    """

    try:
        probe = getattr(db, "in_transaction", None)
        if not callable(probe):
            return True
        active = probe()
        if inspect.isawaitable(active):  # a mocked session: no connection
            if inspect.iscoroutine(active):
                active.close()
            return True
        if active is not True:
            return True
        pending = [
            name for name in ("new", "dirty", "deleted") if getattr(db, name, ())
        ]
        if pending:
            logger.warning(
                WRITE_DROPPED_LOG,
                alert="credit_observational_write",
                reason="route_pending_writes",
                pending=pending,
            )
            return False
        rollback = getattr(db, "rollback")
        await rollback()
        return probe() is not True
    except Exception as error:
        logger.warning(
            WRITE_DROPPED_LOG,
            alert="credit_observational_write",
            reason="route_transaction",
            error_type=type(error).__name__,
        )
        return False


@dataclass(frozen=True)
class _Decision:
    anchor_id: UUID
    projection: Mapping[str, Any]
    deliverable: bool


def _claim_key(
    ingress: ObservationalIngress, principal: CreditPrincipal | None
) -> tuple[str | None, str | None] | None:
    """``(idempotency_key, provider_message_id)`` of the claim this decision
    takes, or ``None`` when it takes no claim."""

    if principal is None:
        return None
    if ingress.source == "events":
        # H3: only production (API key) claims, and only with a valid key.
        if (
            ingress.origin is not InteractionOrigin.PRODUCTION
            or not principal.authenticated
            or ingress.idempotency_key is None
        ):
            return None
        return ingress.idempotency_key, None
    if ingress.provider_message_id is None:
        return None
    return None, ingress.provider_message_id


async def _invalidate_connection(connection: AsyncConnection) -> None:
    with suppress(Exception):
        await connection.invalidate()


def _pool_exhausted(session: AsyncSession) -> bool:
    """``True`` when checking a connection out of the session's pool would
    wait: a ``QueuePool`` with nothing checked in and its overflow used up.

    Read on the event loop right before the checkout, with no await in
    between; a race lost to another thread still ends inside the budget.
    Any other pool (``StaticPool``, ``NullPool``), an unbound or mocked
    session: ``False`` (nothing to wait for, or nothing to inspect).
    """

    try:
        pool = getattr(session.get_bind(), "pool", None)
    except Exception:
        return False
    if not isinstance(pool, QueuePool):
        return False
    max_overflow = getattr(pool, "_max_overflow", -1)
    if not isinstance(max_overflow, int) or max_overflow < 0:
        return False  # unbounded overflow never waits
    return pool.checkedin() == 0 and pool.checkedout() >= pool.size() + max_overflow


def _single_connection_pool(session: AsyncSession) -> bool:
    """``True`` for a pool that holds ONE shared connection (``StaticPool``,
    ``SingletonThreadPool``: SQLite ``:memory:``), never PostgreSQL's."""

    try:
        pool = getattr(session.get_bind(), "pool", None)
    except Exception:
        return False
    return isinstance(pool, (StaticPool, SingletonThreadPool))


def _session_factory() -> Callable[[], AsyncSession]:
    from src.repositories.database import get_db_session_factory

    return get_db_session_factory()


async def _write(
    ingress: ObservationalIngress,
    decision: _Decision,
    factory: Callable[[], AsyncSession],
    deadline: float,
) -> ObservationalOutcome:
    """One write within the budget ending at ``deadline`` (loop time).

    The budget is ONE ``asyncio.timeout_at`` around checkout, statements,
    commit AND the session close -- the close's ROLLBACK (the ``enforced``,
    ``tenant_gone`` and failure paths end with an open transaction) is a
    round trip too, so it never runs unbounded after the budget.  The one
    exception is a single-connection pool (SQLite ``:memory:``): cancelling a
    statement there makes SQLAlchemy invalidate the pool's only connection,
    which destroys the database.  There the same deadline is checked between
    the steps instead (``TimeoutError``, rolled back by the close, a local
    call), never cancelling a statement in flight.
    """

    session = factory()
    held: list[AsyncConnection] = []
    single = _single_connection_pool(session)
    loop = asyncio.get_running_loop()

    def due() -> None:
        if single and loop.time() >= deadline:
            raise TimeoutError

    if single:
        try:
            return await _write_steps(ingress, decision, session, due, held)
        finally:
            await session.close()
    async with asyncio.timeout_at(deadline):
        try:
            return await _write_steps(ingress, decision, session, due, held)
        finally:
            await _close_within_budget(session, held)


async def _close_within_budget(
    session: AsyncSession, held: list[AsyncConnection]
) -> None:
    """Close ``session`` inside the caller's budget.

    A close that does not finish -- cancelled (the budget, or the request)
    with the ROLLBACK maybe in flight, or failed with an ordinary error --
    invalidates the checked-out connection (never returned to the pool in an
    unknown state) and releases the session without another round trip
    before the error propagates (the budget turns a cancellation into
    ``TimeoutError``).  The invalidation is never itself bounded: cancelling
    it would leave SQLAlchemy without returning the connection (a leak).
    """

    try:
        await session.close()
    except BaseException:
        for connection in held:
            await _invalidate_connection(connection)
        with suppress(Exception):
            await session.close()  # invalidated: no round trip left
        raise


async def _write_steps(
    ingress: ObservationalIngress,
    decision: _Decision,
    session: AsyncSession,
    due: Callable[[], None],
    held: list[AsyncConnection],
) -> ObservationalOutcome:
    connection: AsyncConnection | None = None
    try:
        if _pool_exhausted(session):
            return "pool_exhausted"  # never queue for the request's pool
        connection = await session.connection()
        held.append(connection)  # for the close (see _close_within_budget)
        due()
        if connection.dialect.name == "postgresql":
            await session.execute(
                text(_SET_SERVER_TIMEOUTS), {"timeout": OBSERVATIONAL_SERVER_TIMEOUT}
            )
        repository = CreditRepository(session)
        if await repository.enforcement_at() is not None:
            return "enforced"
        due()
        # Lock order step 1: the tenant row FOR SHARE before its claim/effect.
        scope = await repository.tenant_scope(ingress.tenant_id, lock=True)
        tenant = (
            None
            if scope is None
            else await read_tenant_builder(session, ingress.tenant_id)
        )
        if scope is None or tenant is None:
            return "tenant_gone"
        due()
        principal = (
            None
            if ingress.identity is None
            else derive_credit_principal(ingress.identity, tenant)
        )
        authenticated = principal is not None and principal.authenticated
        interaction_id = decision.anchor_id
        claim_key = _claim_key(ingress, principal)
        if claim_key is not None and principal is not None:
            idempotency_key, provider_message_id = claim_key
            claimed = await repository.insert_ingress_claim(
                tenant_id=ingress.tenant_id,
                builder_id=tenant.builder_id,
                source=ingress.source,
                principal_id=principal.principal_id,
                idempotency_key=idempotency_key,
                provider_message_id=provider_message_id,
                interaction_id=decision.anchor_id,
                payload_sha256=ingress.payload_sha256,
                decision_version=OBSERVATIONAL_DECISION_VERSION,
                session_version=await repository.session_message_count(
                    ingress.tenant_id, ingress.session_id
                ),
                mode=OBSERVATIONAL_MODE,
            )
            if claimed is None:
                return "tenant_gone"
            interaction_id = claimed
            due()
        inserted = await repository.insert_observational_effect(
            tenant_id=ingress.tenant_id,
            builder_id=tenant.builder_id,
            effect_id=trace_effect_id(ingress.tenant_id, interaction_id),
            effect_kind=TRACE_EFFECT_KIND,
            trace_id=interaction_id,
            origin=ingress.origin.value,
            authenticated=authenticated,
            would_debit_mc=would_debit_mc(
                origin=ingress.origin,
                authenticated=authenticated,
                deliverable=decision.deliverable,
            ),
            decision_digest=decision_digest(
                tenant_id=ingress.tenant_id,
                interaction_id=interaction_id,
                decision_version=OBSERVATIONAL_DECISION_VERSION,
                origin=ingress.origin,
                decision=decision.projection,
            ),
        )
        due()
        await session.commit()
        return "recorded" if inserted else "replayed"
    except asyncio.CancelledError:
        # The budget (or the request) was cancelled, maybe mid-statement:
        # this connection never goes back to the pool -- unless it IS the
        # pool's only connection (SQLite ``:memory:``), where invalidating
        # it would destroy the database; that one is rolled back by close.
        # (A single-connection pool runs without the timeout, so only the
        # request's own cancellation can land there.)
        if connection is not None and not _single_connection_pool(session):
            await _invalidate_connection(connection)
        raise


async def record_observational_trace(
    ingress: ObservationalIngress,
    *,
    anchor_id: UUID,
    decision: Mapping[str, Any],
    deliverable: bool,
    session_factory: Callable[[], AsyncSession] | None = None,
    budget_seconds: float = OBSERVATIONAL_WRITE_BUDGET_SECONDS,
) -> ObservationalOutcome:
    """Best-effort claim and observational effect of one decision.

    Bounded by ONE budget of ``budget_seconds`` (checkout, statements,
    commit; see :func:`_write`).  Never raises except a cancellation of the
    caller.
    """

    record = _Decision(anchor_id, decision, deliverable)
    error_type: str | None = None
    deadline = asyncio.get_running_loop().time() + budget_seconds
    try:
        factory = session_factory or _session_factory()
        outcome = await _write(ingress, record, factory, deadline)
    except TimeoutError:
        outcome = "timeout"
        error_type = "TimeoutError"
    except Exception as error:
        outcome = "failed"
        error_type = type(error).__name__
    else:
        if outcome in ("recorded", "replayed"):
            return outcome
        if outcome == "enforced":
            # The expected steady state after the enforcement (simulator and
            # unknown traffic still reach the writer): not an alert.
            logger.info(
                WRITE_DROPPED_LOG,
                reason=outcome,
                source=ingress.source,
                tenant_id=str(ingress.tenant_id),
            )
            return outcome
    logger.warning(
        WRITE_DROPPED_LOG,
        alert="credit_observational_write",
        reason=outcome,
        source=ingress.source,
        tenant_id=str(ingress.tenant_id),
        error_type=error_type,
    )
    # Group 7: the original per-tenant warning stays diagnostic-only.  The
    # exported counter deliberately accepts only the closed technical reason
    # vocabulary and is itself inert while the credit switch is ineffective.
    from src.services.credit_rollout import record_observational_write_drop

    record_observational_write_drop(outcome)
    return outcome


async def record_events_decision(
    request: HTTPConnection,
    db: object,
    *,
    canonical_request: CanonicalEventRequest,
    context: ResolvedInteractionContext,
    response: object,
) -> ObservationalOutcome | None:
    """``POST /v1/events`` after phase 2: one trace for the decision, with or
    without an LLM answer (D5).  ``None`` when nothing was attempted."""

    ingress = events_ingress(
        request, canonical_request=canonical_request, context=context
    )
    if ingress is None:
        return None
    return await _record_response(ingress, db, response, deliverable=True)


async def record_webhook_decision(
    ingress: ObservationalIngress | None, db: object, response: object
) -> ObservationalOutcome | None:
    """A webhook decision that went through the pipeline; it would cost a
    trace only with a deliverable response."""

    if ingress is None:
        return None
    return await _record_response(
        ingress, db, response, deliverable=deliverable_response(response)
    )


async def _record_response(
    ingress: ObservationalIngress,
    db: object,
    response: object,
    *,
    deliverable: bool,
) -> ObservationalOutcome | None:
    decided = response_decision(response)
    if decided is None:
        logger.warning(
            WRITE_DROPPED_LOG,
            alert="credit_observational_write",
            reason="no_anchor",
            source=ingress.source,
            tenant_id=str(ingress.tenant_id),
        )
        return None
    if not await end_route_transaction(db):
        return "route_busy"
    event_id, projection = decided
    return await record_observational_trace(
        ingress, anchor_id=event_id, decision=projection, deliverable=deliverable
    )


async def record_meta_file_handoff(
    ingress: ObservationalIngress | None,
    db: object,
    handoff: object,
    *,
    note: str,
) -> ObservationalOutcome | None:
    """The Meta file handoff (forward-on-request and ``audio_too_long``, O2):
    the handoff is the decision, anchored on its handoff id."""

    if ingress is None:
        return None
    handoff_id = getattr(handoff, "id", None)
    if not isinstance(handoff_id, UUID):
        logger.warning(
            WRITE_DROPPED_LOG,
            alert="credit_observational_write",
            reason="no_anchor",
            source=ingress.source,
            tenant_id=str(ingress.tenant_id),
        )
        return None
    if not await end_route_transaction(db):
        return "route_busy"
    return await record_observational_trace(
        ingress,
        anchor_id=handoff_id,
        decision={
            "kind": "meta_file_handoff",
            "handoff_id": str(handoff_id),
            "note": note,
        },
        deliverable=True,
    )


# ---------------------------------------------------------------------------
# Enforced ``POST /v1/events`` (Group 3b-1).
# ---------------------------------------------------------------------------

ENFORCED_MODE: Final = "enforced"
# The enforced executor never re-decides either: a takeover re-executes the
# SAME decision version (OWNER-f), the fences make a bump unnecessary.
ENFORCED_DECISION_VERSION: Final = 1
# The two effects of zero (M7, D2, LOW-8): written at insert, no ledger
# entry.  ``trace_no_response`` is the webhooks' (Group 3b-2).
CREDIT_PENDING_EFFECT_KIND: Final = "trace_credit_pending"
NO_RESPONSE_EFFECT_KIND: Final = "trace_no_response"
# ``SET LOCAL lock_timeout`` of the enforced phase 1 only (OWNER-2).
PHASE1_LOCK_TIMEOUT_SECONDS: Final = 15
# Components of the worst case the claim lease covers (OWNER-1), pinned and
# recomputed by test from the models they come from:
#   * the synchronous status webhook of ``enrich_scheduling_verification``
#     inside phase 1: ``WebhookConfig`` allows retry_count <= 5 attempts of
#     timeout_seconds <= 120 with a backoff of retry_delay_seconds <= 60 x
#     2^(k-1) between them -- 5 x 120 + 60 x (1 + 2 + 4 + 8) = 1500 s;
#   * PHASE1_LOCK_TIMEOUT_SECONDS = 15 s;
#   * the LLM: two primary calls and one fallback of ``LLMClient``'s 30 s;
#   * the final transaction.
LEASE_WEBHOOK_SECONDS: Final = 1500
LEASE_LLM_SECONDS: Final = 90
LEASE_FINAL_TRANSACTION_SECONDS: Final = 75
# LOW-4: never DECREASE this value on a running fleet without draining it
# first: a request still holding a claim under the longer lease can pass its
# final check while a retry, judging the claim with the new shorter lease,
# already answered the final ``event_failed``.  Increasing it is safe.
CLAIM_LEASE_SECONDS: Final = 1680
# ``Retry-After`` of ``event_in_progress``: min(lease left, 5 s), >= 1 s.
RETRY_AFTER_MAX_SECONDS: Final = 5

IDEMPOTENCY_KEY_REQUIRED: Final = "idempotency_key_required"
IDEMPOTENCY_KEY_INVALID: Final = "idempotency_key_invalid"
CREDIT_PENDING_CODE: Final = "credit_pending"
DUPLICATE_EVENT_CODE: Final = "duplicate_event"
PAYLOAD_CONFLICT_CODE: Final = "payload_conflict"
EVENT_IN_PROGRESS_CODE: Final = "event_in_progress"
EVENT_FAILED_CODE: Final = "event_failed"
PAYLOAD_CONFLICT_LOG: Final = "credit_payload_conflict"
ENFORCED_LOG: Final = "credit enforced decision"

CREDIT_SUSPENDED_CODE: Final = "credit_suspended"
PrecheckOutcome = Literal["ok", "credit_pending", "credit_suspended", "tenant_gone"]


class CreditTenantGone(Exception):
    """The tenant row vanished under an enforced transaction (a concurrent
    ``DELETE /clinics/{clinic_id}``): nothing of it may be written."""


class CreditEnforcementUnavailable(Exception):
    """This process already saw ``credit_enforcement_at`` set and can no
    longer read it: the request is answered 503, never as ``main`` (D1)."""


CREDIT_ENFORCEMENT_UNAVAILABLE_CODE: Final = "credit_enforcement_unavailable"
ENFORCEMENT_UNREADABLE_AFTER_ENFORCED: Final = "enforcement_unreadable_after_enforced"
# D1 (owner, option B): the enforcement is one-way.  Once THIS process has
# read ``credit_enforcement_at`` set, a later failure to read it can no
# longer mean "not enforced yet": production traffic gets a 503 instead of
# ``main``'s free path.  Before the process ever saw it set, a failed read
# keeps ``main``'s path (dominance).  Per process, never persisted.
_enforcement_seen = False


def enforcement_seen() -> bool:
    """Whether this process has read ``credit_enforcement_at`` set."""

    return _enforcement_seen


def forget_enforcement_seen() -> None:
    """Back to a process that never saw the enforcement (tests only)."""

    global _enforcement_seen
    _enforcement_seen = False


def claim_lease() -> timedelta:
    """The claim lease, read at call time (``CLAIM_LEASE_SECONDS``)."""

    return timedelta(seconds=CLAIM_LEASE_SECONDS)


def replay_retention() -> timedelta:
    """``credit_replay_retention_hours`` (OWNER-d), read at call time."""

    return timedelta(hours=settings.credit_replay_retention_hours)


def retry_after_seconds(remaining: float) -> int:
    """``min(seconds left of the lease, RETRY_AFTER_MAX_SECONDS)``, at least
    1 (HTTP ``Retry-After`` is whole seconds)."""

    return max(1, min(RETRY_AFTER_MAX_SECONDS, math.ceil(remaining)))


@dataclass(frozen=True)
class EventsEnforcement:
    """One ``POST /v1/events`` request captured as ENFORCED: the switch on at
    entry, ``credit_enforcement_at`` set, origin ``production`` with an
    authenticated principal.  Decided once per request."""

    tenant_id: UUID
    session_id: UUID
    principal_id: str
    payload_sha256: str
    idempotency_key: str | None
    key_problem: str | None


@dataclass(frozen=True)
class ClaimAnswer:
    """An answer read from the claim's state table (item 4-vi)."""

    status: int
    body: Mapping[str, Any]
    retry_after: int | None = None
    replay: bool = False


@dataclass(frozen=True)
class ClaimLease:
    """The lease this executor holds: ``observed`` is the claim's
    ``created_at`` as returned, the ``:observed`` of every fence."""

    claim_id: UUID
    interaction_id: UUID
    observed: datetime


@dataclass(frozen=True)
class DecisionCharge:
    """The effect one enforced decision wrote: a debit (``trace``) or an
    effect of zero (``trace_credit_pending``), under ``builder_id``."""

    effect_id: str
    builder_id: UUID
    effect_kind: str
    debited_mc: int = 0
    debt_mc: int = 0


@dataclass(frozen=True)
class Phase1Outcome:
    result: Any
    clinic_config: Any
    message_history: Any
    session_version: int
    charge: DecisionCharge | None
    handoff_id: UUID | None
    # Webhooks (3b-2, OD-2): the session's pause state as phase 1 left it --
    # the decision's own handoff pause included -- read under its lock.
    session_pause: Any = None


@dataclass(frozen=True)
class FinalOutcome:
    payload: Mapping[str, Any]
    interaction_event_id: UUID | None
    charge: DecisionCharge | None
    # The response-outbox row the final transaction wrote (``None``: none).
    outbox_id: UUID | None = None


def _answer(status: int, code: str, **extra: Any) -> ClaimAnswer:
    retry_after = extra.pop("retry_after", None)
    return ClaimAnswer(
        status=status, body={"code": code, **extra}, retry_after=retry_after
    )


def _validated_key(raw: str | None) -> tuple[str | None, str | None]:
    """``(key, problem)``: absent ⇒ ``idempotency_key_required``; empty,
    longer than 255 or outside visible ASCII ⇒ ``idempotency_key_invalid``."""

    if raw is None:
        return None, IDEMPOTENCY_KEY_REQUIRED
    key = valid_ingress_id(raw)
    if key is None:
        return None, IDEMPOTENCY_KEY_INVALID
    return key, None


async def capture_events_enforcement(
    request: HTTPConnection,
    db: object,
    *,
    tenant: object,
    canonical_request: CanonicalEventRequest,
    context: ResolvedInteractionContext,
) -> EventsEnforcement | None:
    """Decide ONCE whether this request runs the enforced path.

    ``None`` -- ``main``'s path and the Group 3a writer -- unless the request
    captured the switch on at entry, its origin is ``production`` with an
    authenticated principal, the route's session is a real one and
    ``credit_enforcement_at`` (read once, here, with no write) is set.
    ``simulator`` and ``unknown`` never get here.  A failed read is logged
    (an alert) and keeps the request observational -- before the enforcement
    a credit-side failure never changes ``main``'s answer -- unless this
    process already saw ``credit_enforcement_at`` set: the enforcement is
    one-way, so then it raises :class:`CreditEnforcementUnavailable` (the
    route answers 503) with its own alert (D1).
    """

    global _enforcement_seen

    if not request_credit_switch(request):
        return None
    if context.origin is not InteractionOrigin.PRODUCTION:
        return None
    if not isinstance(db, AsyncSession):
        return None
    identity = request_credit_identity(request) or identity_from_tenant_context(tenant)
    if identity is None or identity.tenant_id != context.effective_tenant_id:
        return None
    principal = derive_credit_principal(
        identity,
        TenantBuilder(tenant_id=identity.tenant_id, builder_id=None, is_active=True),
    )
    if not principal.authenticated:
        return None
    try:
        enforced_at = await CreditRepository(db).enforcement_at()
    except Exception as error:
        with suppress(Exception):
            await db.rollback()
        if _enforcement_seen:
            logger.error(
                ENFORCED_LOG,
                alert="credit_enforcement_unavailable",
                reason=ENFORCEMENT_UNREADABLE_AFTER_ENFORCED,
                tenant_id=str(context.effective_tenant_id),
                error_type=type(error).__name__,
            )
            raise CreditEnforcementUnavailable(
                "credit_enforcement_at was set and can no longer be read"
            ) from error
        logger.warning(
            ENFORCED_LOG,
            alert="credit_enforcement_read",
            reason="enforcement_unreadable",
            tenant_id=str(context.effective_tenant_id),
            error_type=type(error).__name__,
        )
        return None
    if not isinstance(enforced_at, datetime):
        return None
    _enforcement_seen = True
    key, problem = _validated_key(request.headers.get(IDEMPOTENCY_KEY_HEADER))
    return EventsEnforcement(
        tenant_id=context.effective_tenant_id,
        session_id=context.session_id,
        principal_id=principal.principal_id,
        payload_sha256=event_payload_sha256(canonical_request),
        idempotency_key=key,
        key_problem=problem,
    )


async def session_in_tenant(db: AsyncSession, enforcement: EventsEnforcement) -> bool:
    """The session belongs to the tenant: a plain read, no lock, no write."""

    return await CreditRepository(db).session_in_tenant(
        enforcement.tenant_id, enforcement.session_id
    )


def _require_key(enforcement: EventsEnforcement) -> str:
    if enforcement.idempotency_key is None:
        raise ValueError("an enforced execution needs its Idempotency-Key")
    return enforcement.idempotency_key


async def read_events_claim(
    db: AsyncSession, enforcement: EventsEnforcement, *, may_take_over: bool = True
) -> ClaimAnswer | ClaimRow | None:
    """Read the key's claim in a short transaction and answer by the table.

    ``None``: no claim (insert one).  A :class:`ClaimRow`: the lease expired
    before phase 1 -- take it over (only when ``may_take_over``: a reader
    that lost a fence never takes over by itself, it answers
    ``event_in_progress`` and lets the next retry do it).  Otherwise the
    :class:`ClaimAnswer`.  A different payload is ``payload_conflict`` before
    any lock.  The reader that may answer ``event_in_progress`` or
    ``event_failed`` takes the claim ``FOR SHARE`` -- waiting for a final
    transaction in flight -- and only then reads the outbox, in a NEW
    statement, so it never answers ``event_failed`` for a decision that
    commits (HIGH-1).
    """

    repository = CreditRepository(db)
    try:
        claim = await repository.idempotency_claim(
            enforcement.tenant_id,
            principal_id=enforcement.principal_id,
            idempotency_key=_require_key(enforcement),
        )
        if claim is None:
            await db.commit()
            return None
        if claim.payload_sha256 != enforcement.payload_sha256:
            await db.commit()
            logger.warning(
                PAYLOAD_CONFLICT_LOG,
                tenant_id=str(enforcement.tenant_id),
                interaction_id=str(claim.interaction_id),
                stored_payload_sha256=claim.payload_sha256,
                request_payload_sha256=enforcement.payload_sha256,
            )
            return _answer(409, PAYLOAD_CONFLICT_CODE)
        if claim.mode != ENFORCED_MODE:
            # Claimed before the enforcement (observational): a duplicate,
            # never executed, replayed or charged again.
            await db.commit()
            return _answer(
                409, DUPLICATE_EVENT_CODE, interaction_id=str(claim.interaction_id)
            )
        locked = await repository.claim_for_share(claim.id)
        if locked is None:
            await db.commit()
            return None
        now = await repository.lease_clock()
        outbox = await repository.response_outbox_row(
            enforcement.tenant_id,
            trace_effect_id(
                enforcement.tenant_id, locked.interaction_id, ENFORCED_DECISION_VERSION
            ),
        )
        await db.commit()
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    return _claim_state_answer(locked, outbox, now, may_take_over=may_take_over)


def _claim_state_answer(
    claim: ClaimRow,
    outbox: ResponseOutboxRow | None,
    now: datetime,
    *,
    may_take_over: bool,
) -> ClaimAnswer | ClaimRow:
    interaction = str(claim.interaction_id)
    if outbox is not None:
        if not outbox.payload or now >= outbox.created_at + replay_retention():
            return _answer(409, DUPLICATE_EVENT_CODE, interaction_id=interaction)
        return ClaimAnswer(status=200, body=dict(outbox.payload), replay=True)
    remaining = (claim.lease_started_at + claim_lease() - now).total_seconds()
    if remaining > 0:
        return _answer(
            409, EVENT_IN_PROGRESS_CODE, retry_after=retry_after_seconds(remaining)
        )
    if claim.session_version is None:
        if may_take_over:
            return claim
        return _answer(409, EVENT_IN_PROGRESS_CODE, retry_after=1)
    # Phase 1 committed, the lease is over: FINAL, never re-executed (OWNER-a/f).
    return _answer(409, EVENT_FAILED_CODE, interaction_id=interaction)


async def precheck_events(
    db: AsyncSession, enforcement: EventsEnforcement
) -> PrecheckOutcome:
    """The 402 pre-check, in a short transaction, before any LLM.

    Tenant ``FOR SHARE``; a standalone tenant (no builder, no account) is
    ``credit_pending``; a builder gets its account on demand -- a builder
    that never paid is served as debt, never ``credit_pending`` -- and a
    quarantine hold is ``credit_pending``.  ``credit_pending`` rolls back:
    nothing is written (not even the account).
    """

    repository = CreditRepository(db)
    try:
        scope = await repository.tenant_scope(enforcement.tenant_id, lock=True)
        if scope is None:
            await db.rollback()
            return "tenant_gone"
        if scope.builder_id is None:
            await db.rollback()
            return "credit_pending"
        await repository.ensure_account(scope.builder_id)
        if await repository.quarantine_hold(scope.builder_id):
            await db.rollback()
            return "credit_pending"
        # Group 7: a monthly allowance participates only through an exact
        # current binding (or bounded active grace); a reconciled unexpired
        # top-up is still independently usable after cancellation.  A
        # missing coverage is a reconciliation boundary, not exhaustion --
        # exhaustion itself continues through the normal debt lifecycle.
        from src.services.credit_rollout import CreditRolloutService

        if not (
            await CreditRolloutService(db).binding_coverage(
                scope.builder_id, quarantine_checked=True
            )
        ).usable:
            await db.rollback()
            return "credit_pending"
        # Group 5: the agent gate -- past the 72 h deadline the episode is
        # evaluated under the account lock (same effect ids as the worker);
        # its transitions commit with this transaction.
        gate = await credit_lifecycle.request_gate(db, scope.builder_id)
        await db.commit()
        if gate == "suspended":
            logger.info(
                ENFORCED_LOG,
                reason=CREDIT_SUSPENDED_CODE,
                tenant_id=str(enforcement.tenant_id),
            )
            return "credit_suspended"
        return "ok"
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise


async def acquire_events_claim(
    db: AsyncSession,
    enforcement: EventsEnforcement,
    *,
    takeover: ClaimRow | None,
) -> ClaimLease | ClaimAnswer:
    """Insert the claim (its own short transaction, after the pre-check), or
    take an expired one over before phase 1.  A lost race re-reads the
    claim and answers by the table, never taking it over by itself."""

    repository = CreditRepository(db)
    try:
        if takeover is None:
            scope = await repository.tenant_scope(enforcement.tenant_id, lock=True)
            if scope is None:
                raise CreditTenantGone("the tenant vanished before its claim")
            row = await repository.insert_enforced_claim(
                tenant_id=enforcement.tenant_id,
                builder_id=scope.builder_id,
                principal_id=enforcement.principal_id,
                idempotency_key=_require_key(enforcement),
                interaction_id=uuid4(),
                payload_sha256=enforcement.payload_sha256,
                decision_version=ENFORCED_DECISION_VERSION,
            )
            await db.commit()
            if row is not None:
                return ClaimLease(row.id, row.interaction_id, row.created_at)
        else:
            observed = await repository.take_over_claim(
                takeover.id, observed=takeover.created_at, lease=claim_lease()
            )
            await db.commit()
            if observed is not None:
                logger.info(
                    ENFORCED_LOG,
                    reason="claim_taken_over",
                    tenant_id=str(enforcement.tenant_id),
                    interaction_id=str(takeover.interaction_id),
                )
                return ClaimLease(takeover.id, takeover.interaction_id, observed)
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    return await reread_events_claim(db, enforcement)


async def reread_events_claim(
    db: AsyncSession, enforcement: EventsEnforcement
) -> ClaimAnswer:
    """After a lost fence or race: re-read the claim and answer by the
    table, without ever taking it over."""

    verdict = await read_events_claim(db, enforcement, may_take_over=False)
    if isinstance(verdict, ClaimAnswer):
        return verdict
    # The claim vanished (its tenant was deleted) or is takeover material:
    # the caller retries.
    return _answer(409, EVENT_IN_PROGRESS_CODE, retry_after=1)


async def release_events_lease(
    db: AsyncSession,
    enforcement: EventsEnforcement,
    lease: ClaimLease,
    *,
    session_version: int | None,
) -> None:
    """A known failure expires the executor's own lease through the same
    fence (MEDIUM-1), in a short transaction of its own: the next retry
    takes over at once (before phase 1) or answers ``event_failed`` at once
    (after it).  A failure here is only logged."""

    try:
        with suppress(Exception):
            await db.rollback()
        released = await CreditRepository(db).release_claim_lease(
            lease.claim_id,
            observed=lease.observed,
            session_version=session_version,
            lease=claim_lease(),
        )
        await db.commit()
    except Exception as error:
        with suppress(Exception):
            await db.rollback()
        logger.warning(
            ENFORCED_LOG,
            alert="credit_lease_release",
            reason="lease_release_failed",
            tenant_id=str(enforcement.tenant_id),
            interaction_id=str(lease.interaction_id),
            error_type=type(error).__name__,
        )
        return
    logger.info(
        ENFORCED_LOG,
        reason="lease_released" if released else "lease_release_fenced",
        tenant_id=str(enforcement.tenant_id),
        interaction_id=str(lease.interaction_id),
        phase1=session_version is not None,
    )


def _handoff_id(result: object) -> UUID | None:
    summary = getattr(result, "handoff_summary", None)
    raw = summary.get("id") if isinstance(summary, Mapping) else None
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def phase1_decision(
    result: object, handoff_id: UUID, *, kind: str = "events_phase1_handoff"
) -> dict[str, Any]:
    """The PII-free projection of a phase 1 decision that handed off
    (``kind``: ``events_phase1_handoff``, or ``{source}_phase1_handoff`` for
    a webhook)."""

    event_id = getattr(result, "event_id", None)
    projection: dict[str, Any] = {
        "kind": kind,
        "event_id": str(event_id) if event_id is not None else None,
        "handoff_id": str(handoff_id),
    }
    for name in (
        "intent",
        "intent_category",
        "funnel_stage",
        "action",
        "safety_concern",
        "safety_category",
        "handoff_required",
        "handoff_reason",
        "mode",
    ):
        projection[name] = _text(_enum_value(getattr(result, name, None)))
    return projection


async def _charge_decision(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    builder_id: UUID,
    effect_id: str,
    digest: str,
) -> DecisionCharge:
    """Account ``FOR UPDATE`` -> hold -> lots ``FOR UPDATE`` -> one trace of
    debit (debt for what the capacity does not cover, O5), or -- under a
    hold that appeared after the pre-check -- an effect of zero
    ``trace_credit_pending`` (D2): the decision is delivered, the ledger
    does not change."""

    result = await CreditLedger(db).debit(
        builder_id,
        effect_id,
        TRACE_COST_MC,
        at=datetime.now(timezone.utc),
        effect_kind=TRACE_EFFECT_KIND,
        tenant_id=tenant_id,
        decision_digest=digest,
    )
    if result.status == "credit_pending":
        await CreditRepository(db).insert_effect(
            builder_id=builder_id,
            tenant_id=tenant_id,
            effect_id=effect_id,
            effect_kind=CREDIT_PENDING_EFFECT_KIND,
            decision_digest=digest,
        )
        logger.info(
            ENFORCED_LOG,
            reason="late_hold",
            tenant_id=str(tenant_id),
            effect_id=effect_id,
        )
        return DecisionCharge(effect_id, builder_id, CREDIT_PENDING_EFFECT_KIND)
    return DecisionCharge(
        effect_id,
        builder_id,
        TRACE_EFFECT_KIND,
        debited_mc=result.debited_mc,
        debt_mc=result.debt_mc,
    )


async def run_enforced_phase1(
    db: AsyncSession,
    enforcement: EventsEnforcement,
    lease: ClaimLease,
    *,
    run_pipeline: Callable[[], Awaitable[tuple[Any, Any, Any]]],
) -> Phase1Outcome | None:
    """Phase 1 of an enforced decision, in the route's session ``db``.

    ``SET LOCAL lock_timeout`` -> tenant ``FOR SHARE`` -> ``run_pipeline``
    (the session ``FOR UPDATE``; a synchronous handoff writes its record and
    ``handoff.request`` in this transaction, #145) -> with a handoff: the
    decision's debit (or the effect of zero under a hold, the handoff goes
    on) and ``credit_effect_id`` merged into the handoff's context -> the
    phase 1 marker, LAST -> commit.  ``None`` when the marker's fence did
    not hold: everything (state, handoff, debit) was rolled back and the
    creation webhook never leaves.  Any exception propagates with the
    transaction still open: the caller rolls back and releases the lease.
    """

    async def mark(repository: CreditRepository, session_version: int) -> bool:
        # The lease is read at the marker (CLAIM_LEASE_SECONDS at call time).
        return await repository.mark_claim_phase1(
            lease.claim_id,
            observed=lease.observed,
            session_version=session_version,
            lease=claim_lease(),
        )

    return await _run_phase1(
        db,
        tenant_id=enforcement.tenant_id,
        session_id=enforcement.session_id,
        interaction_id=lease.interaction_id,
        run_pipeline=run_pipeline,
        mark=mark,
        lock_timeout=PHASE1_LOCK_TIMEOUT_SECONDS,
        decision_kind="events_phase1_handoff",
    )


async def _run_phase1(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    session_id: UUID,
    interaction_id: UUID,
    run_pipeline: Callable[[], Awaitable[tuple[Any, Any, Any]]],
    mark: Callable[[CreditRepository, int], Awaitable[bool]],
    lock_timeout: int | None,
    decision_kind: str,
    observe: Callable[[CreditRepository], Awaitable[Any]] | None = None,
) -> Phase1Outcome | None:
    """Phase 1 of an enforced decision (``POST /v1/events`` and webhooks):
    ``SET LOCAL lock_timeout`` (only when given) -> tenant ``FOR SHARE`` ->
    ``run_pipeline`` -> the phase 1 handoff's charge and link -> ``mark``
    (the claim's phase 1 marker, LAST) -> commit; ``None`` (rolled back)
    when the marker's fence did not hold."""

    repository = CreditRepository(db)
    if lock_timeout is not None:
        await repository.set_local_lock_timeout(lock_timeout)
    scope = await repository.tenant_scope(tenant_id, lock=True)
    if scope is None:
        raise CreditTenantGone("the tenant vanished before phase 1")
    result, clinic_config, message_history = await run_pipeline()
    effect_id = trace_effect_id(tenant_id, interaction_id, ENFORCED_DECISION_VERSION)
    charge: DecisionCharge | None = None
    handoff_id = _handoff_id(result)
    if handoff_id is not None:
        if scope.builder_id is None:
            logger.warning(
                ENFORCED_LOG,
                alert="credit_enforced_standalone",
                reason="standalone_at_phase1",
                tenant_id=str(tenant_id),
                interaction_id=str(interaction_id),
            )
        else:
            charge = await _charge_decision(
                db,
                tenant_id=tenant_id,
                builder_id=scope.builder_id,
                effect_id=effect_id,
                digest=decision_digest(
                    tenant_id=tenant_id,
                    interaction_id=interaction_id,
                    decision_version=ENFORCED_DECISION_VERSION,
                    origin=InteractionOrigin.PRODUCTION,
                    decision=phase1_decision(result, handoff_id, kind=decision_kind),
                ),
            )
            if not await repository.link_handoff_credit_effect(
                tenant_id=tenant_id,
                handoff_id=handoff_id,
                effect_id=charge.effect_id,
            ):
                raise RuntimeError(
                    "the phase 1 handoff record is not in this transaction"
                )
    session_version = await repository.session_message_count(tenant_id, session_id) or 0
    session_pause = None if observe is None else await observe(repository)
    if not await mark(repository, session_version):
        await db.rollback()
        logger.warning(
            ENFORCED_LOG,
            alert="credit_claim_fence",
            reason="phase1_fence_lost",
            tenant_id=str(tenant_id),
            interaction_id=str(interaction_id),
        )
        return None
    await db.commit()
    return Phase1Outcome(
        result=result,
        clinic_config=clinic_config,
        message_history=message_history,
        session_version=session_version,
        charge=charge,
        handoff_id=handoff_id,
        session_pause=session_pause,
    )


def response_payload(
    response: Any, interaction_event_id: UUID | None
) -> dict[str, Any]:
    """The answer a replay returns: the response JSON, ``interaction_event_id``
    included (the value the response carries after the commit, LOW-7)."""

    updated = response.model_copy(update={"interaction_event_id": interaction_event_id})
    payload: dict[str, Any] = updated.model_dump(mode="json")
    return payload


async def run_enforced_final(
    db: AsyncSession,
    enforcement: EventsEnforcement,
    lease: ClaimLease,
    phase1: Phase1Outcome,
    *,
    response: Any,
    write_bundle: Callable[[str | None], Awaitable[UUID | None]],
) -> FinalOutcome | None:
    """The final transaction (M5): the decision, its effect, the stored
    answer and the fence commit together, or nothing does.

    Tenant ``FOR SHARE`` FIRST (``DELETE /clinics`` locks the tenant and
    cascades into ``sessions``) -> the bundle in this transaction (its
    foreign keys take ``FOR KEY SHARE`` on ``sessions`` and wait for a
    concurrent phase 1 of the same session holding no credit lock) ->
    account -> hold -> lots -> one trace of debit/debt, the reuse of the
    phase 1 charge (decided by phase 1's result, never by a new builder
    lookup), or the effect of zero under a late hold -> the response-outbox
    row (``delivered``) -> the final fence (keeps the claim's row lock to
    the commit) -> commit.  ``None`` when the fence did not hold (rolled
    back: no bundle, no debit).  Exceptions propagate with the transaction
    open: the caller rolls back and releases the lease.
    """

    async def fence(repository: CreditRepository) -> bool:
        return await repository.hold_claim_final_fence(
            lease.claim_id,
            observed=lease.observed,
            session_version=phase1.session_version,
            lease=claim_lease(),
        )

    return await _run_final(
        db,
        tenant_id=enforcement.tenant_id,
        interaction_id=lease.interaction_id,
        phase1=phase1,
        response=response,
        write_bundle=write_bundle,
        deliverable=True,  # D5: a POST /v1/events decision always costs a trace
        stored_answer=lambda interaction_event_id: (
            response_payload(response, interaction_event_id),
            "delivered",
        ),
        fence=fence,
    )


async def _run_final(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    interaction_id: UUID,
    phase1: Phase1Outcome,
    response: Any,
    write_bundle: Callable[[str | None], Awaitable[UUID | None]],
    deliverable: bool,
    stored_answer: Callable[[UUID | None], tuple[dict[str, Any], str] | None],
    fence: Callable[[CreditRepository], Awaitable[bool]],
) -> FinalOutcome | None:
    """The final transaction shared by ``POST /v1/events`` and the webhooks.

    Tenant ``FOR SHARE`` -> bundle -> one trace of debit/debt (or the reuse
    of phase 1's charge, an effect of zero under a late hold, or -- for a
    decision that is not ``deliverable`` -- the effect of zero
    ``trace_no_response``) -> the response-outbox row ``stored_answer``
    returns (payload and state; none when it returns ``None``) -> ``fence``
    -> commit.
    """

    repository = CreditRepository(db)
    scope = await repository.tenant_scope(tenant_id, lock=True)
    if scope is None:
        raise CreditTenantGone("the tenant vanished before the final transaction")
    effect_id = trace_effect_id(tenant_id, interaction_id, ENFORCED_DECISION_VERSION)
    charge = phase1.charge
    # A phase 1 that handed off already decided this decision's charge (its
    # effect and builder, or none for a tenant standalone at phase 1): the
    # final transaction reuses that outcome and never looks the builder up
    # again, so a reparent between the phases never charges twice (LOW-5).
    decided_in_phase1 = phase1.handoff_id is not None
    if charge is not None:
        credit_effect_id: str | None = charge.effect_id
    elif not decided_in_phase1 and scope.builder_id is not None:
        credit_effect_id = effect_id
    else:
        credit_effect_id = None
    interaction_event_id = await write_bundle(credit_effect_id)
    if not decided_in_phase1:
        if scope.builder_id is None:
            # LOW-5: the tenant turned standalone since the pre-check.
            logger.warning(
                ENFORCED_LOG,
                alert="credit_enforced_standalone",
                reason="standalone_at_final",
                tenant_id=str(tenant_id),
                interaction_id=str(interaction_id),
            )
        else:
            decided = response_decision(response)
            projection = decided[1] if decided is not None else {"kind": "pipeline"}
            digest = decision_digest(
                tenant_id=tenant_id,
                interaction_id=interaction_id,
                decision_version=ENFORCED_DECISION_VERSION,
                origin=InteractionOrigin.PRODUCTION,
                decision=projection,
            )
            if deliverable:
                charge = await _charge_decision(
                    db,
                    tenant_id=tenant_id,
                    builder_id=scope.builder_id,
                    effect_id=effect_id,
                    digest=digest,
                )
            else:
                charge = await _no_response_effect(
                    db,
                    tenant_id=tenant_id,
                    builder_id=scope.builder_id,
                    effect_id=effect_id,
                    digest=digest,
                )
    stored = stored_answer(interaction_event_id)
    payload: dict[str, Any] = {}
    outbox_id: UUID | None = None
    if stored is not None:
        payload, state = stored
        outbox_id = await repository.insert_response_outbox(
            tenant_id=tenant_id,
            effect_id=effect_id,
            payload=payload,
            state=state,
        )
    if not await fence(repository):
        await db.rollback()
        logger.warning(
            ENFORCED_LOG,
            alert="credit_claim_fence",
            reason="final_fence_lost",
            tenant_id=str(tenant_id),
            interaction_id=str(interaction_id),
        )
        return None
    await db.commit()
    return FinalOutcome(
        payload=payload,
        interaction_event_id=interaction_event_id,
        charge=charge,
        outbox_id=outbox_id,
    )


async def _no_response_effect(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    builder_id: UUID,
    effect_id: str,
    digest: str,
) -> DecisionCharge:
    """``trace_no_response`` (MEDIUM-3, OWNER-e; webhooks only): a decision
    with nothing deliverable -- every LLM attempt failed, or the builder has
    no LLM, and there was no deterministic answer nor handoff -- is an effect
    of zero, written at insert, with no ledger entry."""

    repository = CreditRepository(db)
    await repository.ensure_account(builder_id)
    await repository.insert_effect(
        builder_id=builder_id,
        tenant_id=tenant_id,
        effect_id=effect_id,
        effect_kind=NO_RESPONSE_EFFECT_KIND,
        decision_digest=digest,
    )
    logger.info(
        ENFORCED_LOG,
        reason="no_response",
        tenant_id=str(tenant_id),
        effect_id=effect_id,
    )
    return DecisionCharge(effect_id, builder_id, NO_RESPONSE_EFFECT_KIND)


# ---------------------------------------------------------------------------
# Enforced Z-API and Meta webhooks, the Meta file handoff and the response
# outbox (Group 3b-2).
# ---------------------------------------------------------------------------

WebhookSource = Literal["zapi", "meta"]
WebhookPrecheck = Literal["credit_pending", "credit_suspended"]

# The 200 bodies of the enforced webhook paths (the providers retry non-2xx).
CREDIT_PENDING_SKIP: Final[Mapping[str, Any]] = {
    "ok": True,
    "skipped": CREDIT_PENDING_CODE,
}
# Group 5: a builder suspended after its 72 h of debt (the agent does not run).
CREDIT_SUSPENDED_SKIP: Final[Mapping[str, Any]] = {
    "ok": True,
    "skipped": CREDIT_SUSPENDED_CODE,
}
DUPLICATE_MESSAGE_SKIP: Final[Mapping[str, Any]] = {
    "ok": True,
    "skipped": "duplicate_message",
}
PAYLOAD_CONFLICT_SKIP: Final[Mapping[str, Any]] = {
    "ok": True,
    "skipped": PAYLOAD_CONFLICT_CODE,
}

# Delivery of a stored reply (OD-1/OD-2/OD-5, owner decisions of the 3b-2
# review).  ONE ``asyncio.timeout`` bounds the PROVIDER CALL only
# (``ReplySender.send``: ``_send_reply_with_optional_tts`` with TTS and its
# text fallback).  Its worst case is recomputed by test from the real httpx
# timeouts of the senders and the TTS providers, one connect + write + read
# per request: TTS 10+45+45 = 100 s, then the audio (Z-API 5+30+30 = 65 s;
# Meta upload 65 s + send 5+15+15 = 35 s), then the text fallback (35 s):
# Z-API 200 s, Meta 235 s.  A timeout is an UNCERTAIN result: never resent.
REPLY_SEND_TIMEOUT_SECONDS: Final = 240
# The bookkeeping after an accepted send (the echo id tracked and stored, the
# conversation marked unread) has its own bound and is shielded; its failure
# only logs -- the reply IS delivered once the provider returned.
REPLY_RECORD_TIMEOUT_SECONDS: Final = 30
# The publisher never takes a ``pending`` row younger than this: the inline
# send of a fresh row has a whole send timeout plus a margin first (pinned).
PUBLISHER_MIN_AGE_MARGIN_SECONDS: Final = 30
PUBLISHER_MIN_AGE_SECONDS: Final = 300
# A row left ``sending`` (a crash between the claim and the mark) is
# reclaimed after this many seconds since its claim: longer than a send plus
# its bookkeeping (pinned).
SENDING_LEASE_SECONDS: Final = 330
# OD-1: the publisher only recovers the CRASH windows (commit -> send, send ->
# mark) and sends one row at most ONCE: a failed send -- inline or recovered
# -- is ``failed`` at once (main's behaviour: the patient never gets it
# later), and a row the publisher already took (attempts >= 1) that comes
# back is ``failed`` without a send.
PUBLISHER_MAX_ATTEMPTS: Final = 1
PUBLISHER_POLL_SECONDS: Final = 10.0
# Rows the publisher delivers per tick, one at a time.
PUBLISHER_BATCH: Final = 20
# OD-2: a stored reply older than this (since its decision) is never sent;
# longer than the publisher's recovery windows (pinned).
REPLY_MAX_AGE_SECONDS: Final = 600
# The retention sweep (OWNER-d, OD-3): its own lifespan task, every interval
# (first tick one interval after start-up), in batches of this many rows.
RETENTION_SWEEP_INTERVAL_SECONDS: Final = 300.0
RETENTION_SWEEP_BATCH: Final = 500
# The personal data of a stored webhook reply, erased as soon as the row is
# ``delivered`` or ``failed`` (OWNER-3); the whole payload goes after the
# retention.
REPLY_PII_KEYS: Final = ("phone", "text")
REPLY_PAYLOAD_KIND: Final = "webhook_reply.v1"
PUBLISHER_LOG: Final = "credit response outbox"


class CreditReplyUndeliverable(Exception):
    """The send adapter refused a stored reply before any provider call
    (missing credentials, an unusable payload); ``reason`` is the stable
    code the webhook answers with."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReplySender(Protocol):
    """The send adapter of a stored reply (``src.api.routes.webhook``),
    injected by its caller -- the route inline, the lifespan for the
    publisher -- so this module never imports the route (LOW-13).  Three
    steps, so only the provider call is under the send timeout (OD-5)."""

    async def prepare(self, payload: Mapping[str, Any], db: AsyncSession) -> Any:
        """Read what the send needs (the tenant's credentials) with ``db``;
        no provider call.  Raises :class:`CreditReplyUndeliverable`."""

    async def send(self, prepared: Any) -> Any:
        """The provider call only (``_send_reply_with_optional_tts``);
        raises when nothing was sent."""

    async def record(self, prepared: Any, sent: Any, db: AsyncSession) -> None:
        """Main's bookkeeping after an accepted send (the echo id tracked and
        stored, the conversation marked unread on a handoff)."""


@dataclass(frozen=True)
class WebhookEnforcement:
    """One Z-API/Meta message captured as ENFORCED by its adapter's
    pre-check (switch on at entry, ``production`` with an authenticated
    principal, ``credit_enforcement_at`` set, an account and no hold)."""

    source: WebhookSource
    tenant_id: UUID
    principal_id: str
    provider_message_id: str
    payload_sha256: str


def provider_claim_id(raw: str) -> str:
    """The storable claim id of a provider message: the id itself when it is
    1..255 visible ASCII characters, else ``sha256:<hex>`` of its UTF-8 (an
    enforced decision is never left without its claim)."""

    valid = valid_ingress_id(raw)
    if valid is not None:
        return valid
    return "sha256:" + sha256_hex(raw.encode("utf-8"))


async def precheck_webhook(
    request: HTTPConnection,
    db: object,
    *,
    source: WebhookSource,
    tenant_id: UUID,
    identity: Callable[[], AuthenticatedIdentity | None],
    provider_message_id: str,
    payload_sha256: Callable[[], str],
) -> WebhookEnforcement | WebhookPrecheck | None:
    """The per-adapter pre-check (MEDIUM-4, D4, OWNER-c), the same as 3b-1's.

    ``None`` -- main's path and the Group 3a writer -- unless the request
    captured the switch on at entry, the route's session is a real one, the
    principal is authenticated and ``credit_enforcement_at`` (read ONCE,
    here) is set.  Then, in the same short transaction: tenant ``FOR SHARE``
    -> a standalone tenant is ``credit_pending`` -> the builder's account on
    demand -> a quarantine hold is ``credit_pending`` -> commit.
    ``credit_pending`` rolls back: nothing is written, nothing is sent.  A
    failed read keeps main's path (logged) unless this process already saw
    the enforcement set: then :class:`CreditEnforcementUnavailable` (D1) and
    the webhook answers 503 -- which in practice drops the message: main's
    replay claim (Redis, before this pre-check) is already spent, so the
    provider's retry answers ``duplicate_message``.  Uazapi and Evolution
    never call this.
    """

    global _enforcement_seen

    try:
        # Like the Group 3a ingress, the capture never raises: a caller
        # without a request scope (a direct call, a stub) is main's path.
        if not request_credit_switch(request):
            return None
        if not isinstance(db, AsyncSession):
            return None
        principal_identity = identity()
        if principal_identity is None or principal_identity.tenant_id != tenant_id:
            return None
        principal = derive_credit_principal(
            principal_identity,
            TenantBuilder(tenant_id=tenant_id, builder_id=None, is_active=True),
        )
    except Exception as error:
        logger.warning(
            ENFORCED_LOG,
            alert="credit_enforcement_read",
            reason="request_unreadable",
            source=source,
            tenant_id=str(tenant_id),
            error_type=type(error).__name__,
        )
        return None
    if not principal.authenticated:
        return None
    repository = CreditRepository(db)
    try:
        enforced_at = await repository.enforcement_at()
    except Exception as error:
        with suppress(Exception):
            await db.rollback()
        if _enforcement_seen:
            logger.error(
                ENFORCED_LOG,
                alert="credit_enforcement_unavailable",
                reason=ENFORCEMENT_UNREADABLE_AFTER_ENFORCED,
                source=source,
                tenant_id=str(tenant_id),
                error_type=type(error).__name__,
            )
            raise CreditEnforcementUnavailable(
                "credit_enforcement_at was set and can no longer be read"
            ) from error
        logger.warning(
            ENFORCED_LOG,
            alert="credit_enforcement_read",
            reason="enforcement_unreadable",
            source=source,
            tenant_id=str(tenant_id),
            error_type=type(error).__name__,
        )
        return None
    if not isinstance(enforced_at, datetime):
        return None
    _enforcement_seen = True
    try:
        scope = await repository.tenant_scope(tenant_id, lock=True)
        if scope is None or scope.builder_id is None:
            await db.rollback()
            return "credit_pending"
        await repository.ensure_account(scope.builder_id)
        if await repository.quarantine_hold(scope.builder_id):
            await db.rollback()
            return "credit_pending"
        from src.services.credit_rollout import CreditRolloutService

        if not (
            await CreditRolloutService(db).binding_coverage(
                scope.builder_id, quarantine_checked=True
            )
        ).usable:
            await db.rollback()
            return "credit_pending"
        # Group 5: the agent gate (see ``precheck_events``).
        gate = await credit_lifecycle.request_gate(db, scope.builder_id)
        await db.commit()
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    if gate == "suspended":
        logger.info(
            ENFORCED_LOG,
            reason=CREDIT_SUSPENDED_CODE,
            source=source,
            tenant_id=str(tenant_id),
        )
        return "credit_suspended"
    return WebhookEnforcement(
        source=source,
        tenant_id=tenant_id,
        principal_id=principal.principal_id,
        provider_message_id=provider_claim_id(provider_message_id),
        payload_sha256=payload_sha256(),
    )


async def precheck_zapi(
    request: HTTPConnection, db: object, *, tenant_id: UUID, body: Mapping[str, Any]
) -> WebhookEnforcement | WebhookPrecheck | None:
    """Z-API: the capability token proved the tenant; ``body.messageId``."""

    return await precheck_webhook(
        request,
        db,
        source="zapi",
        tenant_id=tenant_id,
        identity=lambda: credit_identity("zapi", tenant_id),
        provider_message_id=str(body.get("messageId") or "").strip(),
        payload_sha256=lambda: zapi_payload_sha256(body),
    )


async def precheck_meta(
    request: HTTPConnection, db: object, *, tenant_id: UUID, message: Mapping[str, Any]
) -> WebhookEnforcement | WebhookPrecheck | None:
    """Meta: the HMAC identity of the middleware; ``messages[0].id``."""

    return await precheck_webhook(
        request,
        db,
        source="meta",
        tenant_id=tenant_id,
        identity=lambda: request_credit_identity(request),
        provider_message_id=str(message.get("id") or "").strip(),
        payload_sha256=lambda: provider_message_sha256(message),
    )


async def _claimed_answer(
    db: AsyncSession, enforcement: WebhookEnforcement
) -> Mapping[str, Any]:
    """A redelivery, whatever the first claim's state (in progress,
    abandoned, phase 1 committed, decided or observational): ALWAYS
    ``duplicate_message`` -- never a takeover, never a second reply (the
    recovery of a committed decision is the publisher's) --, or
    ``payload_conflict`` (logged) when the stable fields differ."""

    try:
        claim = await CreditRepository(db).provider_claim(
            enforcement.tenant_id,
            source=enforcement.source,
            provider_message_id=enforcement.provider_message_id,
        )
        await db.commit()
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    if claim is not None and claim.payload_sha256 != enforcement.payload_sha256:
        logger.warning(
            PAYLOAD_CONFLICT_LOG,
            source=enforcement.source,
            tenant_id=str(enforcement.tenant_id),
            interaction_id=str(claim.interaction_id),
            stored_payload_sha256=claim.payload_sha256,
            request_payload_sha256=enforcement.payload_sha256,
        )
        return PAYLOAD_CONFLICT_SKIP
    return DUPLICATE_MESSAGE_SKIP


async def acquire_webhook_claim(
    db: AsyncSession, enforcement: WebhookEnforcement
) -> ClaimLease | Mapping[str, Any]:
    """The webhook claim, a short transaction of its own right before phase 1:
    tenant ``FOR SHARE`` -> ``INSERT ... ON CONFLICT DO NOTHING`` on
    ``(tenant_id, source, provider_message_id)`` -> commit.  A conflict
    answers by :func:`_claimed_answer`.  The PostgreSQL claim is the only
    dedupe authority for charging: ``_claim_incoming_message`` falls back to
    per-process memory when Redis fails outside ``is_production``."""

    repository = CreditRepository(db)
    try:
        scope = await repository.tenant_scope(enforcement.tenant_id, lock=True)
        if scope is None:
            raise CreditTenantGone("the tenant vanished before its claim")
        row = await repository.insert_webhook_claim(
            tenant_id=enforcement.tenant_id,
            builder_id=scope.builder_id,
            source=enforcement.source,
            principal_id=enforcement.principal_id,
            provider_message_id=enforcement.provider_message_id,
            interaction_id=uuid4(),
            payload_sha256=enforcement.payload_sha256,
            decision_version=ENFORCED_DECISION_VERSION,
        )
        await db.commit()
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    if row is None:
        return await _claimed_answer(db, enforcement)
    return ClaimLease(row.id, row.interaction_id, row.created_at)


async def run_webhook_phase1(
    db: AsyncSession,
    enforcement: WebhookEnforcement,
    lease: ClaimLease,
    *,
    session_id: UUID,
    run_pipeline: Callable[[], Awaitable[tuple[Any, Any, Any]]],
) -> Phase1Outcome | None:
    """Phase 1 of an enforced webhook decision in the route's session: the
    same as 3b-1's -- tenant ``FOR SHARE`` -> ``run_pipeline`` (the session
    ``FOR UPDATE``, the synchronous handoff in this transaction) -> the
    handoff's charge and ``credit_effect_id`` -> the marker -- with the
    webhook marker, which has no lease clause (LOW-1), and without
    ``SET LOCAL lock_timeout``: that bound exists to size the ``POST
    /v1/events`` lease (OWNER-1/2) and a webhook has no lease; a webhook
    phase 1 waits for the session like main's.  It also reads the session's
    pause state as phase 1 leaves it (under its lock: the decision's own
    handoff pause included), the snapshot every send of the reply compares
    against (OD-2).  ``None``: the marker did not hold (rolled back)."""

    async def mark(repository: CreditRepository, session_version: int) -> bool:
        return await repository.mark_webhook_claim_phase1(
            lease.claim_id, observed=lease.observed, session_version=session_version
        )

    async def observe(repository: CreditRepository) -> Any:
        return await repository.session_pause_state(enforcement.tenant_id, session_id)

    return await _run_phase1(
        db,
        tenant_id=enforcement.tenant_id,
        session_id=session_id,
        interaction_id=lease.interaction_id,
        run_pipeline=run_pipeline,
        mark=mark,
        lock_timeout=None,
        decision_kind=f"{enforcement.source}_phase1_handoff",
        observe=observe,
    )


async def run_webhook_final(
    db: AsyncSession,
    enforcement: WebhookEnforcement,
    lease: ClaimLease,
    phase1: Phase1Outcome,
    *,
    response: Any,
    write_bundle: Callable[[str | None], Awaitable[UUID | None]],
    reply: Mapping[str, Any] | None,
) -> FinalOutcome | None:
    """The final transaction of an enforced webhook decision: 3b-1's, with
    the webhook final fence (no lease clause) and two differences.  A
    decision with nothing deliverable (no LLM, deterministic answer nor
    handoff) is the effect of zero ``trace_no_response`` (OWNER-e).  The
    response-outbox row is born ``pending`` -- ``created_at =
    clock_timestamp()`` -- with ``reply`` (the send's arguments, never a
    credential) and only when there is a reply to send, plus what every send
    re-checks first (OD-2): ``decided_at`` (the maximum age) and
    ``session_pause`` (the session's pause state as phase 1 left it)."""

    async def fence(repository: CreditRepository) -> bool:
        return await repository.hold_webhook_claim_final_fence(
            lease.claim_id,
            observed=lease.observed,
            session_version=phase1.session_version,
        )

    if reply is not None:
        reply = {
            **reply,
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "session_pause": phase1.session_pause,
        }

    return await _run_final(
        db,
        tenant_id=enforcement.tenant_id,
        interaction_id=lease.interaction_id,
        phase1=phase1,
        response=response,
        write_bundle=write_bundle,
        deliverable=deliverable_response(response),
        stored_answer=lambda _interaction_event_id: (
            None if reply is None else (dict(reply), "pending")
        ),
        fence=fence,
    )


@dataclass(frozen=True)
class FileHandoffOutcome:
    """The enforced Meta file handoff: ``handoff`` (committed with its
    claim, debit and ``credit_effect_id``) or the 200 skip ``answer``."""

    handoff: Any = None
    answer: Mapping[str, Any] | None = None
    charge: DecisionCharge | None = None


def file_handoff_decision(handoff_id: UUID, note: str) -> dict[str, Any]:
    """The PII-free projection of a Meta file handoff decision (O2)."""

    return {"kind": "meta_file_handoff", "handoff_id": str(handoff_id), "note": note}


async def run_meta_file_handoff(
    db: AsyncSession,
    enforcement: WebhookEnforcement,
    *,
    session_id: UUID,
    note: str,
    initiate: Callable[[], Awaitable[Any]],
    before_commit: Callable[[], Awaitable[None]] | None = None,
) -> FileHandoffOutcome:
    """The Meta file handoff as ONE enforced transaction in the route's
    session (O2, MEDIUM-5), in the documented order: tenant ``FOR SHARE`` ->
    session ``FOR UPDATE`` -> account ``FOR UPDATE`` -> hold (rollback,
    ``credit_pending``: no handoff, no ack) -> lots ``FOR UPDATE`` -> the
    enforced claim of ``(tenant_id, 'meta', message_id)`` (a conflict rolls
    back: ``duplicate_message``, no second handoff nor debit) -> ``initiate``
    (``initiate_handoff(..., session=db)``: record and ``handoff.request``
    in this transaction) -> debit/debt -> the effect with its
    ``decision_digest`` -> ``credit_effect_id`` merged into the record's
    context by ``(tenant_id, id)`` -> ``before_commit`` (the forward clears
    the pending file review) -> commit.  No SRM, no LLM: the handoff IS the
    decision.  The caller dispatches the creation webhook after the commit.
    Any exception rolls everything back and propagates."""

    repository = CreditRepository(db)
    tenant_id = enforcement.tenant_id
    try:
        scope = await repository.tenant_scope(tenant_id, lock=True)
        if scope is None:
            raise CreditTenantGone("the tenant vanished before the file handoff")
        session_version = await repository.lock_session(tenant_id, session_id)
        if session_version is None:
            raise CreditTenantGone("the session vanished before the file handoff")
        if scope.builder_id is None:
            # Turned standalone since the pre-check: no account to charge.
            await db.rollback()
            return FileHandoffOutcome(answer=CREDIT_PENDING_SKIP)
        builder_id = scope.builder_id
        await repository.ensure_account(builder_id)
        await repository.lock_account(builder_id)
        if await repository.quarantine_hold(builder_id):
            await db.rollback()
            return FileHandoffOutcome(answer=CREDIT_PENDING_SKIP)
        await repository.lock_lots(builder_id)
        claim = await repository.insert_webhook_claim(
            tenant_id=tenant_id,
            builder_id=builder_id,
            source=enforcement.source,
            principal_id=enforcement.principal_id,
            provider_message_id=enforcement.provider_message_id,
            interaction_id=uuid4(),
            payload_sha256=enforcement.payload_sha256,
            decision_version=ENFORCED_DECISION_VERSION,
            session_version=session_version,
        )
        if claim is None:
            await db.rollback()
            return FileHandoffOutcome(answer=await _claimed_answer(db, enforcement))
        handoff = await initiate()
        handoff_id = getattr(handoff, "id", None)
        if not isinstance(handoff_id, UUID):
            raise RuntimeError("the file handoff returned no record id")
        effect_id = trace_effect_id(
            tenant_id, claim.interaction_id, ENFORCED_DECISION_VERSION
        )
        charge = await _charge_decision(
            db,
            tenant_id=tenant_id,
            builder_id=builder_id,
            effect_id=effect_id,
            digest=decision_digest(
                tenant_id=tenant_id,
                interaction_id=claim.interaction_id,
                decision_version=ENFORCED_DECISION_VERSION,
                origin=InteractionOrigin.PRODUCTION,
                decision=file_handoff_decision(handoff_id, note),
            ),
        )
        if not await repository.link_handoff_credit_effect(
            tenant_id=tenant_id, handoff_id=handoff_id, effect_id=charge.effect_id
        ):
            raise RuntimeError("the file handoff record is not in this transaction")
        if before_commit is not None:
            await before_commit()
        await db.commit()
    except BaseException:
        with suppress(Exception):
            await db.rollback()
        raise
    logger.info(
        ENFORCED_LOG,
        reason="file_handoff",
        tenant_id=str(tenant_id),
        effect_id=charge.effect_id,
        effect_kind=charge.effect_kind,
    )
    return FileHandoffOutcome(handoff=handoff, charge=charge)


def scrub_reply(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The stored reply without its personal data (phone and text)."""

    return {key: value for key, value in payload.items() if key not in REPLY_PII_KEYS}


def _sendable(payload: Mapping[str, Any]) -> bool:
    return (
        payload.get("kind") == REPLY_PAYLOAD_KIND
        and isinstance(payload.get("phone"), str)
        and bool(payload.get("phone"))
        and isinstance(payload.get("text"), str)
        and bool(payload.get("text"))
    )


def _decided_at(payload: Mapping[str, Any]) -> datetime | None:
    raw = payload.get("decided_at")
    if not isinstance(raw, str):
        return None
    try:
        decided = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return decided if decided.tzinfo is not None else None


async def _reply_refusal(
    repository: CreditRepository, payload: Mapping[str, Any]
) -> str | None:
    """OD-2: before ANY send of a stored reply (inline, or recovered by the
    publisher): the reason it must not be sent, or ``None``.  Older than
    :data:`REPLY_MAX_AGE_SECONDS` since its decision; the tenant or session
    gone; the bot turned off; or the session's pause state no longer the one
    phase 1 left (a human takeover, a reactivation, another handoff)."""

    if not _sendable(payload):
        return "payload_unusable"
    decided = _decided_at(payload)
    if decided is None:
        return "payload_unusable"
    age = datetime.now(timezone.utc) - decided
    if age > timedelta(seconds=REPLY_MAX_AGE_SECONDS):
        return "reply_expired"
    try:
        tenant_id = UUID(str(payload["tenant_id"]))
        session_id = UUID(str(payload["session_id"]))
    except (KeyError, ValueError):
        return "payload_unusable"
    state = await repository.reply_guard_state(tenant_id, session_id)
    if state is None:
        return "session_gone"
    bot_active, pause = state
    if not bot_active:
        return "bot_inactive"
    if pause != payload.get("session_pause"):
        return "session_changed"
    return None


InlineOutcome = Literal["delivered", "failed", "not_claimed"]


@dataclass(frozen=True)
class InlineDelivery:
    """What the inline send of one stored reply did."""

    outcome: InlineOutcome
    reason: str | None = None
    error: str | None = None


DeliveryOutcome = Literal["delivered", "failed", "fenced"]


@dataclass(frozen=True)
class _Delivery:
    outcome: DeliveryOutcome
    reason: str | None = None
    error: str | None = None


async def _end_transaction(db: AsyncSession) -> None:
    with suppress(Exception):
        await db.rollback()


async def _mark_failed(
    factory: Callable[[], AsyncSession], claim: OutboxClaim, reason: str
) -> _Delivery:
    """``sending -> failed`` with the PII erased (OWNER-3), and its alert.
    The log never carries the phone nor the text."""

    try:
        async with factory() as session:
            moved = await CreditRepository(session).mark_outbox_failed(
                claim.id, observed=claim.observed, payload=scrub_reply(claim.payload)
            )
            await session.commit()
    except Exception as error:
        # The row stays ``sending``: the publisher takes it after the lease.
        logger.error(
            PUBLISHER_LOG,
            alert="credit_response_failed",
            reason="failed_mark_lost",
            outbox_id=str(claim.id),
            error_type=type(error).__name__,
        )
        return _Delivery("fenced", reason=reason)
    logger.error(
        PUBLISHER_LOG,
        alert="credit_response_failed",
        reason=reason,
        outbox_id=str(claim.id),
        tenant_id=str(claim.tenant_id),
        effect_id=claim.effect_id,
        attempts=claim.attempts,
        fenced=not moved,
    )
    return _Delivery("failed" if moved else "fenced", reason=reason)


async def _record_bounded(
    sender: ReplySender,
    prepared: Any,
    sent: Any,
    factory: Callable[[], AsyncSession],
) -> None:
    """The bookkeeping after an accepted send, in its own session and its own
    bound (:data:`REPLY_RECORD_TIMEOUT_SECONDS`); a failure or a timeout only
    logs.  A statement cut by the bound leaves its connection invalidated,
    never pooled; a single-connection pool (SQLite ``:memory:``) is never
    cut mid-statement (it runs unbounded)."""

    session = factory()
    held: list[AsyncConnection] = []
    single = _single_connection_pool(session)

    async def steps() -> None:
        try:
            held.append(await session.connection())
            await sender.record(prepared, sent, session)
        except BaseException:
            if not single and session.in_transaction():
                for connection in held:
                    await _invalidate_connection(connection)
            raise
        finally:
            with suppress(Exception):
                await session.close()

    try:
        if single:
            await steps()
        else:
            async with asyncio.timeout(REPLY_RECORD_TIMEOUT_SECONDS):
                await steps()
    except Exception as error:
        logger.warning(
            PUBLISHER_LOG,
            alert="credit_response_record",
            reason="record_failed",
            error_type=type(error).__name__,
        )


async def _finish_delivered(
    sender: ReplySender,
    prepared: Any,
    sent: Any,
    claim: OutboxClaim,
    factory: Callable[[], AsyncSession],
) -> _Delivery:
    """After the provider returned: the bookkeeping (bounded, failure only
    logs), then ``sending -> delivered`` with phone and text erased."""

    await _record_bounded(sender, prepared, sent, factory)
    try:
        async with factory() as session:
            moved = await CreditRepository(session).mark_outbox_delivered(
                claim.id, observed=claim.observed, payload=scrub_reply(claim.payload)
            )
            await session.commit()
    except Exception as error:
        # Sent, never marked: the row stays ``sending`` -- the send -> mark
        # window the publisher may recover once (at-least-once).
        logger.error(
            PUBLISHER_LOG,
            alert="credit_response_publisher",
            reason="delivered_mark_lost",
            outbox_id=str(claim.id),
            error_type=type(error).__name__,
        )
        return _Delivery("delivered", reason="mark_lost")
    if not moved:
        # Sent all the same (the provider returned); only the mark lost its
        # fence (the row is gone with its tenant, or was reclaimed).
        logger.warning(
            PUBLISHER_LOG,
            alert="credit_response_publisher",
            reason="delivered_mark_fenced",
            outbox_id=str(claim.id),
        )
        return _Delivery("delivered", reason="mark_fenced")
    return _Delivery("delivered")


async def _prepare_claimed(
    claim: OutboxClaim,
    sender: ReplySender,
    factory: Callable[[], AsyncSession],
) -> tuple[str | None, Any]:
    """Read guards and credentials, then close, within the claim's remaining
    lease minus a full send and bookkeeping budget.  The database clock
    accounts for a slow claim commit; monotonic time bounds every later
    await without depending on the application clock matching PostgreSQL.
    """

    loop = asyncio.get_running_loop()
    reserve = REPLY_SEND_TIMEOUT_SECONDS + REPLY_RECORD_TIMEOUT_SECONDS
    deadline = loop.time() + SENDING_LEASE_SECONDS - reserve
    session = factory()
    held: list[AsyncConnection] = []
    single = _single_connection_pool(session)

    def due() -> None:
        if loop.time() >= deadline:
            raise TimeoutError

    # Never interrupt a statement on SQLite's single shared connection;
    # still refuse to send if a step or its cleanup has consumed the budget.
    async with asyncio.timeout_at(None if single else deadline) as budget:
        try:
            due()
            held.append(await session.connection())
            repository = CreditRepository(session)
            started = loop.time()
            now = await repository.lease_clock()
            observed = claim.observed
            if observed.tzinfo is None:  # SQLite stores UTC without its zone
                observed = observed.replace(tzinfo=timezone.utc)
            remaining = (observed - now).total_seconds() + SENDING_LEASE_SECONDS
            deadline = min(deadline, started + remaining - reserve)
            due()  # reject an expired claim before scheduling its cancellation
            if not single:
                budget.reschedule(deadline)
            refusal = await _reply_refusal(repository, claim.payload)
            due()
            prepared = None
            if refusal is None:
                prepared = await sender.prepare(claim.payload, session)
            due()
            await session.rollback()
        except BaseException:
            # Invalidation must finish: a pending timer must not interrupt
            # it after a synchronous refusal or an already-expired claim.
            if not budget.expired():
                budget.reschedule(None)
            if not single:
                for connection in held:
                    await _invalidate_connection(connection)
            raise
        finally:
            await _close_within_budget(session, held)
    due()  # cleanup must also leave the reserved provider budget intact
    return refusal, prepared


async def _deliver_claimed(
    claim: OutboxClaim,
    sender: ReplySender,
    factory: Callable[[], AsyncSession],
) -> _Delivery:
    """One send of a claimed (``sending``) row, inline or recovered.

    OD-2 first (:func:`_reply_refusal`) and the sender's preparation (the
    credentials), bounded by the remaining lease with send and bookkeeping
    time reserved; then the PROVIDER CALL alone under
    :data:`REPLY_SEND_TIMEOUT_SECONDS` (OD-5).  A refusal, a failure or a
    timeout -- an UNCERTAIN result -- turns the row ``failed`` at once with
    its PII erased and is never resent (OD-1).  Once the provider returned
    the reply IS delivered: the bookkeeping and the ``delivered`` mark run
    shielded from a cancellation of the caller.
    """

    try:
        refusal, prepared = await _prepare_claimed(claim, sender, factory)
    except TimeoutError:
        return await _mark_failed(factory, claim, "prepare_timeout")
    except CreditReplyUndeliverable as refused:
        refusal = refused.reason
    if refusal is not None:
        return await _mark_failed(factory, claim, refusal)
    try:
        async with asyncio.timeout(REPLY_SEND_TIMEOUT_SECONDS):
            sent = await sender.send(prepared)
    except TimeoutError:
        failed = await _mark_failed(factory, claim, "send_uncertain")
        return _Delivery(failed.outcome, reason="send_failed", error="send_timeout")
    except CreditReplyUndeliverable as refused:
        return await _mark_failed(factory, claim, refused.reason)
    except Exception as error:
        failed = await _mark_failed(factory, claim, "send_failed")
        return _Delivery(failed.outcome, reason="send_failed", error=str(error))
    finish = asyncio.ensure_future(
        _finish_delivered(sender, prepared, sent, claim, factory)
    )
    return await asyncio.shield(finish)


async def deliver_reply_inline(
    outbox_id: UUID,
    *,
    sender: ReplySender,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> InlineDelivery:
    """Right after the final commit (H4, LOW-2): claim the row ``pending ->
    sending`` with ``RETURNING`` -- no row means the publisher took it (or
    the row is gone with its tenant), so nothing is sent here -- then one
    send (:func:`_deliver_claimed`): ``delivered``, or ``failed`` at once
    with its PII erased, like main's lost reply (OD-1).  Runs in sessions of
    its own: the route's transaction already ended."""

    factory = session_factory or _session_factory()
    try:
        async with factory() as session:
            claim = await CreditRepository(session).claim_outbox_inline(outbox_id)
            await session.commit()
    except Exception as error:
        logger.warning(
            PUBLISHER_LOG,
            alert="credit_response_inline",
            reason="inline_claim_failed",
            outbox_id=str(outbox_id),
            error_type=type(error).__name__,
        )
        return InlineDelivery("failed", reason="send_failed", error="claim_failed")
    if claim is None:
        logger.info(
            PUBLISHER_LOG, reason="inline_not_claimed", outbox_id=str(outbox_id)
        )
        return InlineDelivery("not_claimed")
    delivery = await _deliver_claimed(claim, sender, factory)
    if delivery.outcome == "delivered":
        return InlineDelivery("delivered")
    return InlineDelivery("failed", reason=delivery.reason, error=delivery.error)


PublisherOutcome = Literal["delivered", "failed", "fenced"]


@dataclass
class PublisherTick:
    """What one publisher tick did (``None`` from :meth:`run_once` when the
    switch was off: nothing was read)."""

    enforced: bool = False
    outcomes: list[PublisherOutcome] = field(default_factory=list)


class CreditResponsePublisher:
    """The relational publisher of ``credit_response_outbox`` (H4, MEDIUM-4),
    which only RECOVERS the crash windows of the inline send (OD-1): a
    ``pending`` row older than :data:`PUBLISHER_MIN_AGE_SECONDS` (a crash
    between the final commit and the inline claim) or a ``sending`` row
    older than :data:`SENDING_LEASE_SECONDS` (a crash between the claim and
    the mark).  It claims nothing with the switch off -- re-read before EVERY
    row -- nor before ``credit_enforcement_at``; claims ONE row at a time in a
    short transaction (``FOR UPDATE SKIP LOCKED``) committed as ``sending``
    before the provider call -- two tasks never send one row at once, and no
    row lock is held across a send --; and sends a row at most ONCE: a row it
    already took (``attempts`` >= :data:`PUBLISHER_MAX_ATTEMPTS`) that comes
    back is ``failed`` without a send, and its own send is
    :func:`_deliver_claimed` (``delivered``, or ``failed`` with the PII
    erased).  The send -> mark window of the inline send may thus deliver a
    reply twice: the one accepted at-least-once case.  The retention sweep is
    :func:`run_retention_sweeper`'s, not this task's (OD-3).
    """

    def __init__(
        self,
        sender: ReplySender,
        *,
        session_factory: Callable[[], AsyncSession] | None = None,
        poll_seconds: float = PUBLISHER_POLL_SECONDS,
        batch: int = PUBLISHER_BATCH,
    ) -> None:
        self._sender = sender
        self._factory = session_factory
        self.poll_seconds = poll_seconds
        self.batch = batch

    def _sessions(self) -> Callable[[], AsyncSession]:
        return self._factory or _session_factory()

    async def run_once(self) -> PublisherTick | None:
        """One tick behind the worker guard (``None``: the switch is off)."""

        return await run_credit_worker_tick(self._tick)

    async def run(self) -> None:
        """The lifespan task: a tick every :attr:`poll_seconds` until
        cancelled; a failed tick is logged (an alert) and the loop goes on."""

        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                await self.run_once()
            except Exception as error:
                logger.error(
                    PUBLISHER_LOG,
                    alert="credit_response_publisher",
                    reason="tick_failed",
                    error_type=type(error).__name__,
                )

    async def _tick(self) -> PublisherTick:
        tick = PublisherTick()
        factory = self._sessions()
        async with factory() as session:
            enforced_at = await CreditRepository(session).enforcement_at()
            await session.rollback()
        if enforced_at is None:
            return tick
        tick.enforced = True
        for _ in range(self.batch):
            if not credit_switch_effective():  # re-read before every row
                break
            claim = await self._claim(factory)
            if claim is None:
                break
            tick.outcomes.append(await self._deliver(factory, claim))
        return tick

    async def _claim(self, factory: Callable[[], AsyncSession]) -> OutboxClaim | None:
        async with factory() as session:
            claim = await CreditRepository(session).claim_outbox_for_publish(
                min_age=timedelta(seconds=PUBLISHER_MIN_AGE_SECONDS),
                sending_lease=timedelta(seconds=SENDING_LEASE_SECONDS),
            )
            await session.commit()
        return claim

    async def _deliver(
        self, factory: Callable[[], AsyncSession], claim: OutboxClaim
    ) -> PublisherOutcome:
        if claim.attempts > PUBLISHER_MAX_ATTEMPTS:
            # The publisher already took this row once (and was cut): its
            # send is uncertain, never repeated.
            delivery = await _mark_failed(factory, claim, "resend_refused")
        else:
            delivery = await _deliver_claimed(claim, self._sender, factory)
        return delivery.outcome


def start_credit_response_publisher(
    sender: ReplySender, source: Settings | None = None
) -> asyncio.Task[None] | None:
    """Start the publisher in the lifespan, only when the configuration is
    on (no task with ``credit_hot_path_enabled`` off, like the switch
    refresher); every row still re-reads the effective switch first."""

    configured = settings if source is None else source
    if configured.credit_hot_path_enabled is not True:
        return None
    return asyncio.create_task(
        CreditResponsePublisher(sender).run(), name="credit-response-publisher"
    )


async def _stop_task(task: asyncio.Task[None] | None, name: str) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as error:
        logger.error(
            PUBLISHER_LOG,
            alert=name,
            reason="stopped_with_error",
            error_type=type(error).__name__,
        )


async def stop_credit_response_publisher(task: asyncio.Task[None] | None) -> None:
    """Cancel the publisher (before the pool closes); never raises.  A row
    cut mid-send stays ``sending``: reclaimed after
    :data:`SENDING_LEASE_SECONDS` and then ``failed`` (OD-1), never resent
    by the publisher."""

    await _stop_task(task, "credit_response_publisher")


# -- the retention sweep (OWNER-d; OD-3) -------------------------------------


async def _outbox_table_exists(session: AsyncSession) -> bool:
    """A database below migration 038 has no outbox (dominance: credit code
    on a ``037`` database works like main).  PostgreSQL only; any other
    dialect has it (``create_all``)."""

    if session.get_bind().dialect.name != "postgresql":
        return True
    found = await session.scalar(
        text("SELECT to_regclass('public.credit_response_outbox') IS NOT NULL")
    )
    return bool(found)


async def sweep_expired_replies(
    session_factory: Callable[[], AsyncSession] | None = None,
) -> int:
    """``payload = '{}'`` on every outbox row that is not ``sending`` and is
    older than ``credit_replay_retention_hours`` -- ``delivered``, ``failed``
    and abandoned ``pending`` ones, of webhooks and of ``POST /v1/events`` --
    in batches of :data:`RETENTION_SWEEP_BATCH`.  OD-3: it runs WHATEVER the
    switch and the config: the one explicit exception to Decision 20, and
    only to ERASE expired personal data -- it never sends nor claims.
    Returns how many rows it emptied."""

    factory = session_factory or _session_factory()
    total = 0
    while True:
        async with factory() as session:
            if not await _outbox_table_exists(session):
                await session.rollback()
                return total
            emptied = await CreditRepository(session).sweep_outbox_payloads(
                retention=replay_retention(), limit=RETENTION_SWEEP_BATCH
            )
            await session.commit()
        total += emptied
        if emptied < RETENTION_SWEEP_BATCH:
            break
    if total:
        logger.info(PUBLISHER_LOG, reason="retention_swept", rows=total)
    return total


async def run_retention_sweeper(
    interval_seconds: float = RETENTION_SWEEP_INTERVAL_SECONDS,
    *,
    session_factory: Callable[[], AsyncSession] | None = None,
) -> None:
    """The lifespan's sweep task: one sweep every ``interval_seconds`` (the
    first one interval after start-up) until cancelled; a failed sweep is
    logged (an alert) and the loop goes on."""

    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await sweep_expired_replies(session_factory)
        except Exception as error:
            logger.error(
                PUBLISHER_LOG,
                alert="credit_retention_sweep",
                reason="sweep_failed",
                error_type=type(error).__name__,
            )


def start_credit_retention_sweeper() -> asyncio.Task[None]:
    """Always started by the lifespan (OD-3), config on or off."""

    return asyncio.create_task(run_retention_sweeper(), name="credit-retention-sweep")


async def stop_credit_retention_sweeper(task: asyncio.Task[None] | None) -> None:
    """Cancel the sweeper before the pool closes; never raises."""

    await _stop_task(task, "credit_retention_sweep")


__all__ = [
    "CREDIT_PENDING_SKIP",
    "DUPLICATE_MESSAGE_SKIP",
    "REPLY_SEND_TIMEOUT_SECONDS",
    "REPLY_RECORD_TIMEOUT_SECONDS",
    "REPLY_MAX_AGE_SECONDS",
    "PAYLOAD_CONFLICT_SKIP",
    "PUBLISHER_BATCH",
    "PUBLISHER_LOG",
    "PUBLISHER_MAX_ATTEMPTS",
    "PUBLISHER_MIN_AGE_MARGIN_SECONDS",
    "PUBLISHER_MIN_AGE_SECONDS",
    "PUBLISHER_POLL_SECONDS",
    "REPLY_PAYLOAD_KIND",
    "REPLY_PII_KEYS",
    "RETENTION_SWEEP_BATCH",
    "RETENTION_SWEEP_INTERVAL_SECONDS",
    "SENDING_LEASE_SECONDS",
    "CreditReplyUndeliverable",
    "CreditResponsePublisher",
    "FileHandoffOutcome",
    "InlineDelivery",
    "PublisherTick",
    "ReplySender",
    "run_retention_sweeper",
    "start_credit_retention_sweeper",
    "stop_credit_retention_sweeper",
    "sweep_expired_replies",
    "WebhookEnforcement",
    "WebhookSource",
    "acquire_webhook_claim",
    "deliver_reply_inline",
    "file_handoff_decision",
    "precheck_meta",
    "precheck_webhook",
    "precheck_zapi",
    "provider_claim_id",
    "run_meta_file_handoff",
    "run_webhook_final",
    "run_webhook_phase1",
    "scrub_reply",
    "start_credit_response_publisher",
    "stop_credit_response_publisher",
    "CLAIM_LEASE_SECONDS",
    "CREDIT_ENFORCEMENT_UNAVAILABLE_CODE",
    "CREDIT_PENDING_CODE",
    "CREDIT_PENDING_EFFECT_KIND",
    "DUPLICATE_EVENT_CODE",
    "ENFORCED_DECISION_VERSION",
    "ENFORCED_MODE",
    "EVENT_FAILED_CODE",
    "EVENT_IN_PROGRESS_CODE",
    "IDEMPOTENCY_KEY_INVALID",
    "IDEMPOTENCY_KEY_REQUIRED",
    "LEASE_FINAL_TRANSACTION_SECONDS",
    "LEASE_LLM_SECONDS",
    "LEASE_WEBHOOK_SECONDS",
    "NO_RESPONSE_EFFECT_KIND",
    "PAYLOAD_CONFLICT_CODE",
    "PAYLOAD_CONFLICT_LOG",
    "PHASE1_LOCK_TIMEOUT_SECONDS",
    "RETRY_AFTER_MAX_SECONDS",
    "ClaimAnswer",
    "ClaimLease",
    "CreditEnforcementUnavailable",
    "CreditTenantGone",
    "DecisionCharge",
    "EventsEnforcement",
    "FinalOutcome",
    "Phase1Outcome",
    "acquire_events_claim",
    "capture_events_enforcement",
    "claim_lease",
    "enforcement_seen",
    "forget_enforcement_seen",
    "phase1_decision",
    "precheck_events",
    "read_events_claim",
    "release_events_lease",
    "replay_retention",
    "reread_events_claim",
    "response_payload",
    "retry_after_seconds",
    "run_enforced_final",
    "run_enforced_phase1",
    "session_in_tenant",
    "DECISION_DIGEST_SCHEMA",
    "IDEMPOTENCY_KEY_HEADER",
    "INGRESS_ID_MAX_LENGTH",
    "OBSERVATIONAL_DECISION_VERSION",
    "OBSERVATIONAL_MODE",
    "OBSERVATIONAL_SERVER_TIMEOUT",
    "OBSERVATIONAL_WRITE_BUDGET_SECONDS",
    "TRACE_COST_MC",
    "TRACE_EFFECT_KIND",
    "WRITE_DROPPED_LOG",
    "IngressSource",
    "ObservationalIngress",
    "ObservationalOutcome",
    "canonical_json",
    "decision_digest",
    "deliverable_response",
    "end_route_transaction",
    "event_payload_sha256",
    "events_ingress",
    "evolution_ingress",
    "meta_ingress",
    "provider_message_sha256",
    "record_events_decision",
    "record_meta_file_handoff",
    "record_observational_trace",
    "record_webhook_decision",
    "response_decision",
    "sha256_hex",
    "trace_effect_id",
    "uazapi_ingress",
    "valid_ingress_id",
    "would_debit_mc",
    "zapi_ingress",
    "zapi_payload_sha256",
]
