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
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.pool import QueuePool, SingletonThreadPool, StaticPool
from starlette.requests import HTTPConnection

from src.api.schemas.events import CanonicalEventRequest
from src.config.logging import get_logger
from src.interaction_context import InteractionOrigin, ResolvedInteractionContext
from src.repositories.credit import CreditRepository
from src.services.credit_ledger import MILLICREDITS_PER_CREDIT
from src.services.credit_principal import (
    AuthenticatedIdentity,
    CreditPrincipal,
    credit_identity,
    derive_credit_principal,
    read_tenant_builder,
    request_credit_identity,
)
from src.services.credit_switch import request_credit_switch

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


__all__ = [
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
