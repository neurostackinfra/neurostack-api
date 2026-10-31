"""Operational authority, legal hold and protected purge (credit Group 5).

Authority (Deliverable 4).  Three credit-only persisted roles --
``production_data_lifecycle_owner``, ``legal_hold_officer``,
``legal_hold_release_officer`` -- granted (append-only) by an authenticated
JWT ``super_admin``, never to the granting user itself; ``super_admin`` is
the JWT role, never an assignment, and never substitutes one.  Every hold
event and every purge also needs a backend-signed, single-use authorization
(HMAC-SHA256 with ``credit_authorization_signing_key``, a ``SecretStr`` of at
least 32 bytes distinct from ``billing_operation_receipt_signing_key``; an
absent, short or reused key fails closed) bound to exactly one purpose,
environment (``settings.environment``), builder, scope, resource, fence
generation and subject user.  Whatever environment, owner or approval id a
caller sends is an EXPECTED value compared with the signed one: it never
grants anything.

Legal hold.  The exact append-only stream ``hold_requested |
hold_activated | hold_released``.  Each event runs in its own
``SERIALIZABLE`` transaction: lock ``credit_hold_generations(builder_id,
scope)`` and every overlapping scope's row (``builder`` first, then the
tenant scopes in order) -> validate assignment, authorization and the
folded state -> consume the authorization's receipt -> append the event ->
generation + 1, exactly once.  Scopes overlap: ``builder`` contains every
``tenant:X`` of the builder, so a purge observes (and fences) the
:func:`coverage_generation` of its scope -- the sum of every overlapping
row -- and folds the holds of every overlapping scope.  ``hold_released`` is also the release receipt: it names the
requester and the activator, and its actor must be neither (and must hold
the separate release role).  A hold intent counts from its REQUEST (intent
before the dispute with a purge).  A scope whose current generation carries
a purge fence is ``closed``: later hold events are refused, never restore
retroactively.

Purge (Deliverables 5-7).  :func:`request_purge` -- the ONLY public entry --
answers ``purge_disabled`` first, before reading or writing anything (no
hold, fence, storage, authorization or receipt is touched), whatever flag,
environment, request or receipt is presented: Groups 1a-7 have no enabling
path; that path and the production conformance are Group 8's.  The machinery
below (:class:`PurgeExecutor`) is real and is exercised only by tests:
prepare (super_admin + active lifecycle-owner assignment + authorization for
the exact fence generation; ``deletion_due`` past its deadline; no effective
hold) -> tombstones/tokens and ``prepared`` jobs -> storage tombstone
(``storage_applied``) -> a NEW ``SERIALIZABLE`` transaction re-reads the
generation and the folded holds and CAS-inserts ``purge_fence(builder, scope,
G)`` only if unchanged (``purge_fenced``), else restores (``held``) -> the
adapter's purge with confirmed absence (``deleted``) or ``held|failed|
blocked`` -> the receipt, written only once every item has a final result.
Crash recovery (:meth:`PurgeExecutor.reconcile`) restores unfenced tokens and
finishes or re-confirms fenced ones; it never records ``deleted`` without
confirmed absence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.config.settings import Settings, settings
from src.models.content_retention import (
    CreditHoldEvent,
    CreditPurgeFence,
    CreditRetentionTombstone,
)
from src.models.credit_object_copy import CreditObjectCopy
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.operational_capability import CreditOperationalAuthorization
from src.models.tenant import Tenant
from src.models.user import User
from src.observability.metric_dimensions import (
    ContractCounter,
    precreate_closed_children,
)
from src.repositories.content_retention import (
    ContentRetentionRepository,
    as_utc,
    hold_scope_order,
)
from src.services.credit_switch import credit_switch_effective
from src.storage.credit_retention_adapter import derived_point_ids

logger = get_logger(__name__)

PURGE_DISABLED: Final = "purge_disabled"
RETENTION_LOG: Final = "credit retention"
MIN_SIGNING_KEY_BYTES: Final = 32
MAX_AUTHORIZATION_TTL: Final = timedelta(hours=24)
LIFECYCLE_OWNER: Final = "production_data_lifecycle_owner"
LEGAL_HOLD_OFFICER: Final = "legal_hold_officer"
LEGAL_HOLD_RELEASE_OFFICER: Final = "legal_hold_release_officer"
ROLES: Final = (LIFECYCLE_OWNER, LEGAL_HOLD_OFFICER, LEGAL_HOLD_RELEASE_OFFICER)
_TENANT_SCOPE: Final = re.compile(
    r"^tenant:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

Purpose = Literal["hold_request", "hold_activate", "hold_release", "purge"]
HoldEventType = Literal["hold_requested", "hold_activated", "hold_released"]
_EVENT_PURPOSE: Final[dict[str, Purpose]] = {
    "hold_requested": "hold_request",
    "hold_activated": "hold_activate",
    "hold_released": "hold_release",
}
_EVENT_ROLE: Final[dict[str, str]] = {
    "hold_requested": LEGAL_HOLD_OFFICER,
    "hold_activated": LEGAL_HOLD_OFFICER,
    "hold_released": LEGAL_HOLD_RELEASE_OFFICER,
}


# Group 7 deliverable 4: hold events and purge requests by closed operation
# and outcome, only with the switch effective (no builder, scope or actor).
CREDIT_RETENTION_OPERATIONS_TOTAL = precreate_closed_children(
    ContractCounter(
        "neurostack_credit_retention_operations_total",
        "Legal hold events and purge requests, by operation and outcome",
        ["operation", "outcome"],
    )
)


def _count_retention(operation: str, outcome: str) -> None:
    if credit_switch_effective():
        CREDIT_RETENTION_OPERATIONS_TOTAL.labels(
            operation=operation, outcome=outcome
        ).inc()


class RetentionRefused(Exception):
    """A refusal with a stable ``code``; raised BEFORE any hold event,
    fence or storage operation (the caller's transaction rolls back)."""

    def __init__(self, code: str, status: int = 403) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _aware(value: datetime, name: str = "instant") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_scope(scope: str) -> str:
    if scope == "builder" or _TENANT_SCOPE.fullmatch(scope):
        return scope
    raise RetentionRefused("scope_invalid", 422)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# The signing key and signed authorizations.
# ---------------------------------------------------------------------------


def signing_key(config: Settings = settings) -> bytes:
    """The authorization key; fails closed when absent, short or shared with
    the ASAAS receipt key."""

    key = config.credit_authorization_signing_key.get_secret_value().encode("utf-8")
    if len(key) < MIN_SIGNING_KEY_BYTES:
        raise RetentionRefused("credit_authorization_key_unavailable", 503)
    billing: Any = getattr(config, "billing_operation_receipt_signing_key", None)
    billing_value = (
        billing.get_secret_value() if hasattr(billing, "get_secret_value") else billing
    )
    if billing_value and hmac.compare_digest(key, str(billing_value).encode("utf-8")):
        raise RetentionRefused("credit_authorization_key_unavailable", 503)
    return key


def key_fingerprint(key: bytes) -> str:
    return sha256_hex(b"credit-authorization-key:" + key)


def authorization_payload(row: CreditOperationalAuthorization) -> dict[str, Any]:
    """The signed fields, canonical (UTC ISO instants, string ids)."""

    return {
        "id": str(row.id),
        "purpose": row.purpose,
        "environment": row.environment,
        "builder_id": str(row.builder_id),
        "scope": row.scope,
        "resource": row.resource,
        "fence_generation": row.fence_generation,
        "subject_user_id": str(row.subject_user_id),
        "issued_by": str(row.issued_by),
        "issued_at": as_utc(row.issued_at).isoformat(),  # type: ignore[union-attr]
        "expires_at": as_utc(row.expires_at).isoformat(),  # type: ignore[union-attr]
    }


def sign(payload: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Actor:
    """The authenticated caller, from the JWT (never from a body)."""

    user_id: UUID
    role: str | None
    jwt: bool

    @property
    def super_admin(self) -> bool:
        return self.jwt and self.role == "super_admin"


def _require_super_admin(actor: Actor) -> None:
    if not actor.super_admin:
        raise RetentionRefused("super_admin_jwt_required")


async def _user_exists(session: AsyncSession, user_id: UUID) -> bool:
    return (await session.scalar(select(User.id).where(User.id == user_id))) is not None


async def grant_assignment(
    session: AsyncSession,
    actor: Actor,
    *,
    role: str,
    user_id: UUID,
    reason: str,
    expires_at: datetime | None = None,
    now: datetime | None = None,
) -> UUID:
    """A ``super_admin`` grants ``role`` to another existing user."""

    _require_super_admin(actor)
    if role not in ROLES:
        raise RetentionRefused("role_invalid", 422)
    if user_id == actor.user_id:
        raise RetentionRefused("self_assignment_forbidden")
    instant = _aware(now or _now())
    if expires_at is not None and _aware(expires_at, "expires_at") <= instant:
        raise RetentionRefused("expires_at_in_past", 422)
    if not reason.strip():
        raise RetentionRefused("reason_required", 422)
    if not await _user_exists(session, user_id):
        raise RetentionRefused("user_not_found", 404)
    row = await ContentRetentionRepository(session).insert_assignment(
        role=role,
        user_id=user_id,
        environment=settings.environment,
        action="granted",
        actor_user_id=actor.user_id,
        reason=reason.strip()[:500],
        expires_at=expires_at,
    )
    return row.id


async def revoke_assignment(
    session: AsyncSession, actor: Actor, *, assignment_id: UUID, reason: str
) -> UUID:
    _require_super_admin(actor)
    repository = ContentRetentionRepository(session)
    granted = await repository.assignment(assignment_id)
    if (
        granted is None
        or granted.action != "granted"
        or granted.environment != settings.environment
    ):
        raise RetentionRefused("assignment_not_found", 404)
    if await repository.revocation_of(assignment_id) is not None:
        raise RetentionRefused("assignment_already_revoked", 409)
    if not reason.strip():
        raise RetentionRefused("reason_required", 422)
    row = await repository.insert_assignment(
        role=granted.role,
        user_id=granted.user_id,
        environment=granted.environment,
        action="revoked",
        revokes_assignment_id=assignment_id,
        actor_user_id=actor.user_id,
        reason=reason.strip()[:500],
    )
    return row.id


async def require_assignment(
    session: AsyncSession, user_id: UUID, role: str, *, now: datetime
) -> None:
    found = await ContentRetentionRepository(session).active_assignment(
        user_id=user_id, role=role, environment=settings.environment, at=now
    )
    if found is None:
        raise RetentionRefused("assignment_required")


@dataclass(frozen=True)
class IssuedAuthorization:
    authorization_id: UUID
    signature: str
    payload: dict[str, Any]


async def issue_authorization(
    session: AsyncSession,
    actor: Actor,
    *,
    purpose: Purpose,
    builder_id: UUID,
    scope: str,
    subject_user_id: UUID,
    resource: str | None = None,
    fence_generation: int | None = None,
    ttl: timedelta = timedelta(hours=1),
    now: datetime | None = None,
    config: Settings = settings,
) -> IssuedAuthorization:
    """Issue one signed single-use authorization (the key fails closed
    BEFORE anything is written)."""

    key = signing_key(config)
    _require_super_admin(actor)
    if purpose not in ("hold_request", "hold_activate", "hold_release", "purge"):
        raise RetentionRefused("purpose_invalid", 422)
    if subject_user_id == actor.user_id:
        raise RetentionRefused("self_authorization_forbidden")
    if (purpose == "purge") != (fence_generation is not None) or (
        fence_generation is not None and fence_generation < 0
    ):
        raise RetentionRefused("fence_generation_invalid", 422)
    if not timedelta(seconds=1) <= ttl <= MAX_AUTHORIZATION_TTL:
        raise RetentionRefused("ttl_invalid", 422)
    scope = normalize_scope(scope)
    if not await _user_exists(session, subject_user_id):
        raise RetentionRefused("user_not_found", 404)
    instant = _aware(now or _now())
    row = CreditOperationalAuthorization(
        id=uuid4(),
        purpose=purpose,
        environment=config.environment,
        builder_id=builder_id,
        scope=scope,
        resource=resource,
        fence_generation=fence_generation,
        subject_user_id=subject_user_id,
        issued_by=actor.user_id,
        issued_at=instant,
        expires_at=instant + ttl,
        key_fingerprint=key_fingerprint(key),
        signature="0" * 64,
    )
    payload = authorization_payload(row)
    row.signature = sign(payload, key)
    await ContentRetentionRepository(session).insert_authorization(
        **{
            column: getattr(row, column)
            for column in (
                "id",
                "purpose",
                "environment",
                "builder_id",
                "scope",
                "resource",
                "fence_generation",
                "subject_user_id",
                "issued_by",
                "issued_at",
                "expires_at",
                "key_fingerprint",
                "signature",
            )
        }
    )
    return IssuedAuthorization(row.id, row.signature, payload)


@dataclass(frozen=True)
class PresentedAuthorization:
    """What a caller presents: the id and the signature it received."""

    authorization_id: UUID
    signature: str


async def verify_authorization(
    session: AsyncSession,
    presented: PresentedAuthorization,
    *,
    actor: Actor,
    purpose: Purpose,
    builder_id: UUID,
    scope: str,
    resource: str | None = None,
    fence_generation: int | None = None,
    now: datetime,
    config: Settings = settings,
) -> CreditOperationalAuthorization:
    """Every binding of the signed row against the operation at hand; any
    mismatch is a refusal BEFORE any hold, fence or storage."""

    key = signing_key(config)
    repository = ContentRetentionRepository(session)
    row = await repository.authorization(presented.authorization_id)
    if row is None:
        raise RetentionRefused("authorization_invalid")
    expected = sign(authorization_payload(row), key)
    if not (
        hmac.compare_digest(expected, row.signature)
        and hmac.compare_digest(expected, str(presented.signature))
        and hmac.compare_digest(row.key_fingerprint, key_fingerprint(key))
    ):
        raise RetentionRefused("authorization_invalid")
    if row.environment != config.environment:
        raise RetentionRefused("authorization_cross_env")
    if row.purpose != purpose:
        raise RetentionRefused("authorization_purpose_mismatch")
    if row.builder_id != builder_id or row.scope != scope:
        raise RetentionRefused("authorization_cross_scope")
    if row.resource != resource or row.fence_generation != fence_generation:
        raise RetentionRefused("authorization_resource_mismatch")
    if row.subject_user_id != actor.user_id:
        raise RetentionRefused("authorization_subject_mismatch")
    issued_at, expires_at = as_utc(row.issued_at), as_utc(row.expires_at)
    assert issued_at is not None and expires_at is not None
    if not issued_at <= now < expires_at:
        raise RetentionRefused("authorization_expired")
    if await repository.authorization_consumed(row.id):
        raise RetentionRefused("authorization_reused", 409)
    return row


# ---------------------------------------------------------------------------
# Legal hold.
# ---------------------------------------------------------------------------


@dataclass
class IntentState:
    intent_id: UUID
    requested: CreditHoldEvent | None = None
    activated: CreditHoldEvent | None = None
    released: CreditHoldEvent | None = None

    @property
    def effective(self) -> bool:
        """A hold counts from its request until its release."""

        return self.requested is not None and self.released is None


def fold_holds(events: Iterable[CreditHoldEvent]) -> dict[UUID, IntentState]:
    intents: dict[UUID, IntentState] = {}
    for event in sorted(events, key=lambda row: (row.generation, str(row.id))):
        state = intents.setdefault(event.intent_id, IntentState(event.intent_id))
        if event.event_type == "hold_requested":
            state.requested = event
        elif event.event_type == "hold_activated":
            state.activated = event
        else:
            state.released = event
    return intents


def hold_effective(events: Iterable[CreditHoldEvent]) -> bool:
    return any(state.effective for state in fold_holds(events).values())


def coverage_generation(scope: str, generations: Mapping[str, int]) -> int:
    """The generation a purge of ``scope`` observes and fences.

    The sum of the hold generations of every scope that overlaps it: for
    ``builder``, every row of the builder (each tenant scope is inside it);
    for ``tenant:X``, the ``builder`` row plus its own.  Every hold event
    increments exactly one row by one and no row ever decreases, so the sum
    is unchanged exactly when no overlapping hold event happened -- a hold on
    ``tenant:X`` moves a builder-wide purge's CAS, and a builder-wide hold
    moves every tenant purge's."""

    if scope == "builder":
        return sum(generations.values())
    return generations.get("builder", 0) + generations.get(scope, 0)


async def _overlapping_scopes(
    session: AsyncSession, builder_id: UUID, scope: str
) -> list[str]:
    """``scope`` and every scope overlapping it, in the fixed lock order:
    ``builder`` and the tenant itself for a tenant scope; ``builder`` and
    every tenant scope of the builder (its clinics, and any scope that
    already has a generation row) for the builder scope."""

    if scope != "builder":
        return hold_scope_order(("builder", scope))
    tenants = await session.execute(
        select(Tenant.id).where(Tenant.builder_id == builder_id)
    )
    existing = await ContentRetentionRepository(session).hold_generations(builder_id)
    return hold_scope_order(
        ["builder", *(f"tenant:{row[0]}" for row in tenants), *existing]
    )


def _closing_fence(
    fences: Iterable[CreditPurgeFence], generations: Mapping[str, int]
) -> bool:
    """A fence closes its scope -- and every overlapping one -- while its
    scope's coverage generation is still the one it fenced."""

    return any(
        fence.generation == coverage_generation(fence.scope, generations)
        for fence in fences
    )


async def _serializable(session: AsyncSession) -> None:
    """Start the session's transaction at SERIALIZABLE (PostgreSQL)."""

    if session.get_bind().dialect.name == "postgresql":
        await session.connection(execution_options={"isolation_level": "SERIALIZABLE"})


def _serialization_failure(error: DBAPIError) -> bool:
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(
            candidate, "pgcode", None
        )
        if state in ("40001", "40P01"):
            return True
    return False


@dataclass(frozen=True)
class HoldResult:
    event_id: UUID
    intent_id: UUID
    event_type: HoldEventType
    generation: int


async def record_hold_event(
    session: AsyncSession,
    actor: Actor,
    *,
    event_type: HoldEventType,
    builder_id: UUID,
    scope: str,
    presented: PresentedAuthorization,
    reason_code: str,
    intent_id: UUID | None = None,
    justification: str | None = None,
    now: datetime | None = None,
    config: Settings = settings,
) -> HoldResult:
    """One hold event in ONE serializable transaction, committed here.

    Refusals (:class:`RetentionRefused`) roll back with nothing written; a
    serialization failure is ``hold_conflict`` (409, retryable)."""

    try:
        await _serializable(session)
        result = await _hold_event_steps(
            session,
            actor,
            event_type=event_type,
            builder_id=builder_id,
            scope=scope,
            presented=presented,
            reason_code=reason_code,
            intent_id=intent_id,
            justification=justification,
            now=_aware(now or _now()),
            config=config,
        )
        await session.commit()
        _count_retention(event_type, "recorded")
        return result
    except DBAPIError as error:
        await session.rollback()
        if _serialization_failure(error):
            raise RetentionRefused("hold_conflict", 409) from error
        raise
    except RetentionRefused as refusal:
        await session.rollback()
        if refusal.status == 409:
            # A policy refusal of the folded hold state (closed scope, intent
            # exists, invalid state, reused authorization).  Validation (422),
            # authorization (403), not-found (404) and the retryable
            # ``hold_conflict`` are a caller's bad or racing request: logged
            # by the route, never the hold-refused alert.
            _count_retention(event_type, "refused")
        raise
    except BaseException:
        await session.rollback()
        raise


def _held_scopes(scope: str) -> tuple[str, ...] | None:
    """The scopes whose holds stop a purge of ``scope``: every scope of the
    builder (``None``) for a builder-wide purge; the builder and the tenant
    for a tenant purge."""

    return None if scope == "builder" else ("builder", scope)


async def _scope_exists(session: AsyncSession, builder_id: UUID, scope: str) -> bool:
    """The builder exists; a tenant scope names one of ITS clinics."""

    from src.models.builder import Builder

    if scope == "builder":
        statement = select(Builder.id).where(Builder.id == builder_id)
    else:
        tenant_id = UUID(scope.split(":", 1)[1])
        statement = select(Tenant.id).where(
            Tenant.id == tenant_id, Tenant.builder_id == builder_id
        )
    return (await session.scalar(statement)) is not None


async def _hold_event_steps(
    session: AsyncSession,
    actor: Actor,
    *,
    event_type: HoldEventType,
    builder_id: UUID,
    scope: str,
    presented: PresentedAuthorization,
    reason_code: str,
    intent_id: UUID | None,
    justification: str | None,
    now: datetime,
    config: Settings,
) -> HoldResult:
    if not actor.jwt:
        raise RetentionRefused("jwt_required")
    scope = normalize_scope(scope)
    if not reason_code.strip():
        raise RetentionRefused("reason_required", 422)
    # The role first: a caller without it never learns whether the builder
    # or the tenant exists (403 before any 404).
    await require_assignment(session, actor.user_id, _EVENT_ROLE[event_type], now=now)
    repository = ContentRetentionRepository(session)
    # Lock every overlapping scope's generation, ``builder`` first: a hold
    # serializes with every fence and hold whose content overlaps its own.
    scopes = await _overlapping_scopes(session, builder_id, scope)
    await repository.lock_hold_generations(builder_id, scopes)
    if not await _scope_exists(session, builder_id, scope):
        raise RetentionRefused("scope_not_found", 404)
    generations = await repository.hold_generations(builder_id)
    generation = generations[scope]
    intents = fold_holds(await repository.hold_events(builder_id, (scope,)))
    # A fence on this scope, on the builder above it or (builder-wide) on a
    # tenant inside it keeps the scope closed while its generation stands.
    if _closing_fence(await repository.fences(builder_id, scopes), generations):
        raise RetentionRefused("scope_closed", 409)
    previous: CreditHoldEvent | None = None
    requested_by: UUID | None = None
    activated_by: UUID | None = None
    if event_type == "hold_requested":
        if intent_id is not None and intent_id in intents:
            raise RetentionRefused("intent_exists", 409)
        intent = intent_id or uuid4()
        resource: str | None = None
    else:
        if intent_id is None or intent_id not in intents:
            raise RetentionRefused("intent_not_found", 404)
        intent = intent_id
        state = intents[intent]
        resource = str(intent)
        if event_type == "hold_activated":
            if state.activated is not None or state.released is not None:
                raise RetentionRefused("hold_state_invalid", 409)
            previous = state.requested
        else:
            if state.activated is None or state.released is not None:
                raise RetentionRefused("hold_state_invalid", 409)
            assert state.requested is not None
            requested_by = state.requested.actor_user_id
            activated_by = state.activated.actor_user_id
            if actor.user_id in (requested_by, activated_by):
                raise RetentionRefused("self_release_forbidden")
            if not (justification or "").strip():
                raise RetentionRefused("justification_required", 422)
            previous = state.activated
    authorization = await verify_authorization(
        session,
        presented,
        actor=actor,
        purpose=_EVENT_PURPOSE[event_type],
        builder_id=builder_id,
        scope=scope,
        resource=resource,
        now=now,
        config=config,
    )
    event_id = uuid4()
    if not await repository.consume_authorization(
        authorization_id=authorization.id,
        consumed_by=actor.user_id,
        consumed_for=event_type,
        result_ref=str(event_id),
        at=now,
    ):
        raise RetentionRefused("authorization_reused", 409)
    after = generation + 1
    fields = {
        "id": str(event_id),
        "intent_id": str(intent),
        "builder_id": str(builder_id),
        "scope": scope,
        "event_type": event_type,
        "reason_code": reason_code.strip()[:80],
        "actor_user_id": str(actor.user_id),
        "occurred_at": now.isoformat(),
        "previous_event_id": str(previous.id) if previous else None,
        "generation": after,
        "authorization_id": str(authorization.id),
        "requested_by": str(requested_by) if requested_by else None,
        "activated_by": str(activated_by) if activated_by else None,
        "justification": (justification or "").strip()[:2000] or None,
    }
    await repository.insert_hold_event(
        id=event_id,
        intent_id=intent,
        builder_id=builder_id,
        scope=scope,
        event_type=event_type,
        reason_code=fields["reason_code"],
        actor_user_id=actor.user_id,
        occurred_at=now,
        previous_event_id=previous.id if previous else None,
        generation=after,
        authorization_id=authorization.id,
        requested_by=requested_by,
        activated_by=activated_by,
        justification=fields["justification"],
        digest=sha256_hex(canonical(fields)),
    )
    await repository.bump_hold_generation(builder_id, scope, generation)
    logger.info(
        RETENTION_LOG,
        hold_event=event_type,
        builder_id=str(builder_id),
        scope=scope,
        generation=after,
    )
    return HoldResult(event_id, intent, event_type, after)


# ---------------------------------------------------------------------------
# Purge: the public entry is disabled in Groups 1a-7.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeOutcome:
    code: str
    operation_id: UUID | None = None


async def request_purge(*_args: Any, **_kwargs: Any) -> PurgeOutcome:
    """Every purge call: ``purge_disabled``, before anything is read or
    written -- no hold, fence, storage, authorization or receipt -- whatever
    flag, environment, request or receipt is presented (Deliverable 7).  The
    enabling path is Group 8's; nothing here can be switched on."""

    logger.info(RETENTION_LOG, outcome=PURGE_DISABLED)
    _count_retention("purge", "disabled")
    return PurgeOutcome(PURGE_DISABLED)


class RetentionStore(Protocol):
    """The storage side of a purge (:mod:`src.storage.credit_retention_adapter`)."""

    async def tombstone(self, item: CreditRetentionTombstone) -> None: ...

    async def restore(self, item: CreditRetentionTombstone) -> None: ...

    async def lookup(self, item: CreditRetentionTombstone) -> Any: ...

    async def purge(self, item: CreditRetentionTombstone) -> str: ...


@dataclass
class PurgeReport:
    operation_id: UUID
    status: str
    outcomes: dict[UUID, str] = field(default_factory=dict)


ScopeTenants = Callable[[AsyncSession, UUID, str], Any]


async def _scope_tenant_ids(
    session: AsyncSession, builder_id: UUID, scope: str
) -> list[UUID]:
    if scope == "builder":
        rows = await session.execute(
            select(Tenant.id).where(Tenant.builder_id == builder_id).order_by(Tenant.id)
        )
        return [row[0] for row in rows]
    tenant_id = UUID(scope.split(":", 1)[1])
    found = await session.scalar(
        select(Tenant.id).where(Tenant.id == tenant_id, Tenant.builder_id == builder_id)
    )
    return [] if found is None else [tenant_id]


class PurgeExecutor:
    """The purge machinery (tests and Group 8 only; never reachable from an
    application request in Groups 1a-7)."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        store: RetentionStore,
        *,
        config: Settings = settings,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.sessions = session_factory
        self.store = store
        self.config = config
        self.clock = clock

    # -- step 1: prepare --------------------------------------------------

    async def prepare(
        self,
        actor: Actor,
        *,
        operation_id: UUID,
        builder_id: UUID,
        scope: str,
        presented: PresentedAuthorization,
        expected_environment: str | None = None,
    ) -> list[UUID]:
        """Validate everything, then write tombstones/tokens and ``prepared``
        jobs, consuming the authorization; returns the tokens."""

        scope = normalize_scope(scope)
        now = _aware(self.clock())
        async with self.sessions() as session:
            try:
                await _serializable(session)
                tokens = await self._prepare(
                    session,
                    actor,
                    operation_id=operation_id,
                    builder_id=builder_id,
                    scope=scope,
                    presented=presented,
                    expected_environment=expected_environment,
                    now=now,
                )
                await session.commit()
                return tokens
            except BaseException:
                await session.rollback()
                raise

    async def _prepare(
        self,
        session: AsyncSession,
        actor: Actor,
        *,
        operation_id: UUID,
        builder_id: UUID,
        scope: str,
        presented: PresentedAuthorization,
        expected_environment: str | None,
        now: datetime,
    ) -> list[UUID]:
        from src.services.credit_lifecycle import builder_state

        _require_super_admin(actor)
        # A caller's environment is only an expected value.
        if (
            expected_environment is not None
            and expected_environment != self.config.environment
        ):
            raise RetentionRefused("environment_mismatch")
        await require_assignment(session, actor.user_id, LIFECYCLE_OWNER, now=now)
        repository = ContentRetentionRepository(session)
        generation = coverage_generation(
            scope, await repository.hold_generations(builder_id)
        )
        authorization = await verify_authorization(
            session,
            presented,
            actor=actor,
            purpose="purge",
            builder_id=builder_id,
            scope=scope,
            resource=str(operation_id),
            fence_generation=generation,
            now=now,
            config=self.config,
        )
        state, episode = await builder_state(session, builder_id)
        due = as_utc(episode.deletion_due_at) if episode is not None else None
        if state != "deletion_due" or due is None or now < due:
            raise RetentionRefused("not_deletion_due", 409)
        if hold_effective(
            await repository.hold_events(builder_id, _held_scopes(scope))
        ):
            raise RetentionRefused("legal_hold_effective", 409)
        if not await repository.consume_authorization(
            authorization_id=authorization.id,
            consumed_by=actor.user_id,
            consumed_for="purge_prepare",
            result_ref=str(operation_id),
            at=now,
        ):
            raise RetentionRefused("authorization_reused", 409)
        tokens: list[UUID] = []
        for tenant_id in await _scope_tenant_ids(session, builder_id, scope):
            for item in await self._items(session, tenant_id):
                token = uuid4()
                await repository.insert_tombstone(
                    token=token,
                    operation_id=operation_id,
                    builder_id=builder_id,
                    tenant_id=tenant_id,
                    scope=scope,
                    observed_generation=generation,
                    **item,
                )
                await repository.insert_job_state(
                    token=token, operation_id=operation_id, state="prepared"
                )
                tokens.append(token)
        return tokens

    async def _items(
        self, session: AsyncSession, tenant_id: UUID
    ) -> list[dict[str, Any]]:
        copies: dict[tuple[str, UUID, str], CreditObjectCopy] = {}
        for copy in (
            await session.execute(
                select(CreditObjectCopy).where(
                    CreditObjectCopy.tenant_id == tenant_id,
                    CreditObjectCopy.state.in_(("ready", "held")),
                )
            )
        ).scalars():
            copies[(copy.source_kind, copy.source_id, copy.content_sha256)] = copy
        items: list[dict[str, Any]] = []
        media = await session.execute(
            select(MediaAsset.id, MediaAsset.sha256, MediaAsset.size_bytes)
            .where(MediaAsset.tenant_id == tenant_id)
            .order_by(MediaAsset.id)
        )
        for row in media:
            digest = bytes(row.sha256).hex()
            items.append(
                self._item("media", row.id, digest, int(row.size_bytes), copies)
            )
        documents = await session.execute(
            select(KnowledgeDocument.id, KnowledgeDocument.content)
            .where(KnowledgeDocument.tenant_id == tenant_id)
            .order_by(KnowledgeDocument.id)
        )
        for document in documents:
            content = str(document.content)
            data = content.encode("utf-8")
            item = self._item(
                "knowledge", document.id, sha256_hex(data), len(data), copies
            )
            item["qdrant_point_ids"] = derived_point_ids(document.id, content)
            items.append(item)
        return items

    @staticmethod
    def _item(
        kind: str,
        source_id: UUID,
        digest: str,
        size: int,
        copies: dict[tuple[str, UUID, str], CreditObjectCopy],
    ) -> dict[str, Any]:
        copy = copies.get((kind, source_id, digest))
        return {
            "source_kind": kind,
            "source_id": source_id,
            "content_sha256": digest,
            "size_bytes": size,
            "s3_object_key": copy.object_key if copy else None,
            "s3_version_id": copy.version_id if copy else None,
            "s3_etag": copy.etag if copy else None,
            "qdrant_point_ids": [],
        }

    # -- step 2: storage tombstone ------------------------------------------

    async def apply_tombstones(self, operation_id: UUID) -> None:
        async with self.sessions() as session:
            items = await ContentRetentionRepository(session).tombstones_of(
                operation_id
            )
        for item in items:
            await self.store.tombstone(item)
            async with self.sessions() as session, session.begin():
                await ContentRetentionRepository(session).insert_job_state(
                    token=item.token, operation_id=operation_id, state="storage_applied"
                )

    # -- step 3: the CAS fence ---------------------------------------------

    async def fence(self, operation_id: UUID) -> UUID | None:
        """New SERIALIZABLE transaction: re-read the generation and the folded
        holds; insert ``purge_fence(builder, scope, G)`` only if unchanged
        and no hold is effective.  ``None``: a hold won -- every token is
        restored and recorded ``held``."""

        async with self.sessions() as session:
            items = await ContentRetentionRepository(session).tombstones_of(
                operation_id
            )
        if not items:
            return None
        first = items[0]
        fence_id: UUID | None = None
        async with self.sessions() as session:
            try:
                await _serializable(session)
                repository = ContentRetentionRepository(session)
                # The builder row, then every overlapping tenant row, in the
                # one fixed order (the same rows every hold event locks).
                await repository.lock_hold_generations(
                    first.builder_id,
                    await _overlapping_scopes(session, first.builder_id, first.scope),
                )
                current = coverage_generation(
                    first.scope,
                    await repository.hold_generations(first.builder_id),
                )
                events = await repository.hold_events(
                    first.builder_id, _held_scopes(first.scope)
                )
                if current == first.observed_generation and not hold_effective(events):
                    fence_id = await repository.insert_fence(
                        builder_id=first.builder_id,
                        scope=first.scope,
                        generation=current,
                        operation_id=operation_id,
                    )
                if fence_id is not None:
                    for item in items:
                        await repository.insert_job_state(
                            token=item.token,
                            operation_id=operation_id,
                            state="purge_fenced",
                            fence_id=fence_id,
                        )
                await session.commit()
            except DBAPIError as error:
                await session.rollback()
                if not _serialization_failure(error):
                    raise
                fence_id = None  # a conflict always aborts the purge
            except BaseException:
                await session.rollback()
                raise
        if fence_id is None:
            await self._restore_all(
                items, outcome="held", detail="hold_won_before_fence"
            )
        return fence_id

    async def _restore_all(
        self, items: Sequence[CreditRetentionTombstone], *, outcome: str, detail: str
    ) -> None:
        for item in items:
            try:
                await self.store.restore(item)
            except Exception as error:
                await self._result(
                    item, "failed", f"restore_failed:{type(error).__name__}"
                )
                continue
            await self._result(item, outcome, detail)

    async def _result(
        self, item: CreditRetentionTombstone, outcome: str, detail: str
    ) -> None:
        async with self.sessions() as session, session.begin():
            await ContentRetentionRepository(session).insert_result(
                token=item.token,
                operation_id=item.operation_id,
                outcome=outcome,
                detail=detail,
            )

    # -- step 4: purge and the receipt ---------------------------------------

    async def purge(self, operation_id: UUID) -> PurgeReport:
        async with self.sessions() as session:
            repository = ContentRetentionRepository(session)
            items = await repository.tombstones_of(operation_id)
            fence = await repository.fence_of(operation_id)
        if fence is None:
            raise RetentionRefused("fence_required", 409)
        for item in items:
            await self._purge_one(item)
        return await self.finish(operation_id)

    async def _purge_one(self, item: CreditRetentionTombstone) -> None:
        async with self.sessions() as session:
            results = await ContentRetentionRepository(session).results_of(item.token)
        if any(result.outcome in ("deleted", "held", "blocked") for result in results):
            return
        try:
            outcome = await self.store.purge(item)
        except Exception as error:
            outcome = "failed"
            detail = f"purge_failed:{type(error).__name__}"
        else:
            detail = f"purge_{outcome}"
        await self._result(item, outcome, detail)

    async def finish(self, operation_id: UUID) -> PurgeReport:
        """The receipt, only once every item has a final result."""

        async with self.sessions() as session, session.begin():
            repository = ContentRetentionRepository(session)
            existing = await repository.receipt(operation_id)
            items = await repository.tombstones_of(operation_id)
            finals: dict[UUID, str] = {}
            for item in items:
                for result in await repository.results_of(item.token):
                    if result.outcome in ("deleted", "held", "blocked"):
                        finals[item.token] = result.outcome
            if existing is not None:
                return PurgeReport(operation_id, existing.status, finals)
            if len(finals) != len(items) or not items:
                return PurgeReport(operation_id, "incomplete", finals)
            counts = {
                outcome: sum(1 for value in finals.values() if value == outcome)
                for outcome in ("deleted", "held", "failed", "blocked")
            }
            if counts["deleted"] == len(items):
                status = "completed"
            elif counts["deleted"]:
                status = "partial"
            else:
                status = "blocked"
            first = items[0]
            await repository.insert_receipt(
                operation_id=operation_id,
                builder_id=first.builder_id,
                scope=first.scope,
                status=status,
                deleted_count=counts["deleted"],
                held_count=counts["held"],
                failed_count=counts["failed"],
                blocked_count=counts["blocked"],
                digest=sha256_hex(
                    canonical(
                        sorted(
                            (str(token), outcome) for token, outcome in finals.items()
                        )
                    )
                ),
            )
            return PurgeReport(operation_id, status, finals)

    # -- crash recovery ------------------------------------------------------

    async def reconcile(self, *, limit: int = 50) -> int:
        """Unfinished tokens (``FOR UPDATE SKIP LOCKED``): without a fence,
        restore (re-checking holds); with one, finish or re-confirm.  Then
        every operation whose results are all final but that has no receipt
        (a crash after the last result, before :meth:`finish`) gets it.
        Returns the number of tokens worked."""

        async with self.sessions() as session:
            repository = ContentRetentionRepository(session)
            unreceipted = await repository.operations_without_receipt(limit=limit)
            tokens = await repository.claim_unfinished_tokens(limit=limit)
            work: list[tuple[CreditRetentionTombstone, bool]] = []
            for token in tokens:
                item = await repository.tombstone(token)
                if item is None:
                    continue
                fenced = "purge_fenced" in await repository.job_states(token)
                work.append((item, fenced))
            # Detach (never expire) what was read: the storage work below runs
            # with no lock held (no lock across a network call); the final
            # result's unique index and the idempotent adapter cover a
            # concurrent recoverer.
            session.expunge_all()
        operations: set[UUID] = set(unreceipted)
        for item, fenced in work:
            operations.add(item.operation_id)
            if fenced:
                await self._purge_one(item)
            else:
                async with self.sessions() as session:
                    held = hold_effective(
                        await ContentRetentionRepository(session).hold_events(
                            item.builder_id, _held_scopes(item.scope)
                        )
                    )
                await self._restore_all(
                    [item],
                    outcome="held" if held else "blocked",
                    detail="restored_without_fence",
                )
        for operation_id in operations:
            await self.finish(operation_id)
        return len(work)


__all__ = [
    "LEGAL_HOLD_OFFICER",
    "LEGAL_HOLD_RELEASE_OFFICER",
    "LIFECYCLE_OWNER",
    "PURGE_DISABLED",
    "Actor",
    "HoldResult",
    "IssuedAuthorization",
    "PresentedAuthorization",
    "PurgeExecutor",
    "PurgeOutcome",
    "PurgeReport",
    "RetentionRefused",
    "fold_holds",
    "grant_assignment",
    "hold_effective",
    "issue_authorization",
    "key_fingerprint",
    "record_hold_event",
    "request_purge",
    "require_assignment",
    "revoke_assignment",
    "sign",
    "signing_key",
    "verify_authorization",
]
