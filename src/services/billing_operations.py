"""Typed, backend-signed operator evidence for billing control-plane actions.

The payment provider is deliberately absent from this module.  These values
are the local, auditable boundary that must be satisfied before a caller can
change a persisted billing state.  Keeping the signing payload closed avoids
accidentally turning an operator receipt into a PII or secret transport.

The server signs ("o servidor assina").  An operator never holds
``BILLING_OPERATION_RECEIPT_SIGNING_KEY`` and never sends or receives a
signature.  Once the route has authenticated the ``super_admin`` JWT that
supplies the actor, :meth:`BillingOperationAuthority.record_receipt` checks
the actor's own active assignment and the nonce, then stores the HMAC-SHA256
of :func:`operation_receipt_message` (operation material, rollout revision,
role, actor, assignment and nonce of that one receipt) in
``signature_sha256``.  Every quorum read (:meth:`~BillingOperationAuthority.
verified_quorum`, :meth:`~BillingOperationAuthority.consume_quorum`)
recomputes it from the stored row, so one receipt's signature never
authenticates another role, actor, assignment, nonce or revision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Final, Iterable, Literal, Sequence, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.billing import (
    BillingOperationAssignment,
    BillingOperationReceipt,
    BillingRolloutControl,
)
from src.observability.metrics import BILLING_OPERATION_RECEIPTS_TOTAL
from src.repositories.billing import billing_advisory_key

# The database CHECK (migration 028) still admits the retired ``migration``
# scope; the code no longer issues or accepts it (Group 10 phase E2).
OperationScope = Literal["rollout", "topup_capability", "incident"]
OperatorRole = Literal["deployer", "approver", "reviewer"]

OPERATOR_ROLES: Final[frozenset[str]] = frozenset({"deployer", "approver", "reviewer"})
OPERATION_SCOPES: Final[frozenset[str]] = frozenset(
    {"rollout", "topup_capability", "incident"}
)
_SHA256_HEX: Final = frozenset("0123456789abcdef")


class BillingOperationRejected(RuntimeError):
    """A fixed, non-sensitive administrative-operation failure."""

    code = "billing_operation_rejected"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


def is_sha256_digest(value: object) -> bool:
    """Return whether *value* is the canonical lowercase SHA-256 encoding."""

    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def require_aware_future(value: datetime, *, now: datetime | None = None) -> None:
    """Reject naive, expired, and zero-lifetime operator evidence."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise BillingOperationRejected("billing_operation_expiry_invalid")
    current = now or datetime.now(timezone.utc)
    if value <= current:
        raise BillingOperationRejected("billing_operation_expired")


def require_receipt_lifetime(expires_at: datetime, *, now: datetime) -> None:
    """Also cap the lifetime: ``expires_at - now`` <= the configured maximum.

    *now* is the creation instant of the assignment or receipt.  The cap is
    ``BILLING_OPERATION_RECEIPT_MAX_TTL_SECONDS`` (default 24 h, at most
    7 days); exactly the cap is accepted.  Readiness rows inherit it because
    they may not outlive the receipts they name.
    """

    require_aware_future(expires_at, now=now)
    cap = timedelta(seconds=settings.billing_operation_receipt_max_ttl_seconds)
    if expires_at - now > cap:
        raise BillingOperationRejected("billing_operation_expiry_too_long")


def operation_payload_digest(
    *,
    scope: OperationScope,
    operation_id: UUID,
    environment: str,
    from_state: str,
    to_state: str,
    budget_digest: str,
    deployment_digest: str,
    expires_at: datetime,
) -> str:
    """Hash the closed receipt payload without encoding identities or secrets."""

    if scope not in OPERATION_SCOPES:
        raise BillingOperationRejected("billing_operation_scope_invalid")
    if not environment or len(environment) > 20:
        raise BillingOperationRejected("billing_operation_environment_invalid")
    if not from_state or not to_state or len(from_state) > 40 or len(to_state) > 40:
        raise BillingOperationRejected("billing_operation_state_invalid")
    if not is_sha256_digest(budget_digest) or not is_sha256_digest(deployment_digest):
        raise BillingOperationRejected("billing_operation_digest_invalid")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise BillingOperationRejected("billing_operation_expiry_invalid")
    canonical = {
        "budget_digest": budget_digest,
        "deployment_digest": deployment_digest,
        "environment": environment,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "from_state": from_state,
        "operation_id": str(operation_id),
        "scope": scope,
        "to_state": to_state,
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _uuid_text(value: object) -> str:
    return str(value if isinstance(value, UUID) else UUID(str(value)))


# The fixed canary budget (``ck_billing_rollout_control_canary_budget``).
CANARY_CHECKOUT_LIMIT: Final = 2
CANARY_SETTLEMENT_LIMIT: Final = 2
CANARY_AMOUNT_LIMIT_CENTS: Final = 109900


def canary_budget_digest(*, environment: str, canary_builder_id: UUID) -> str:
    """The ``budget_digest`` of every ``disabled -> canary_allowlist`` receipt.

    One helper for both sides: the operator tool computes the material's
    ``budget_digest`` with it, and the canary transition refuses any other
    value for the builder it is asked to open.  Every signer of the quorum
    thus signs the builder, and nobody holding the quorum can choose another.

    Canonical encoding (the exact bytes hashed): SHA-256, lowercase hex, of
    the ASCII JSON object with keys sorted, separators ``,`` and ``:`` and no
    whitespace, the builder as lowercase hyphenated UUID text::

        {"canary_amount_limit_cents":109900,"canary_builder_id":"<uuid>",
         "canary_checkout_limit":2,"canary_settlement_limit":2,
         "environment":"<environment>"}

    (shown wrapped; the hashed bytes are one line).
    """

    canonical = {
        "canary_amount_limit_cents": CANARY_AMOUNT_LIMIT_CENTS,
        "canary_builder_id": _uuid_text(canary_builder_id),
        "canary_checkout_limit": CANARY_CHECKOUT_LIMIT,
        "canary_settlement_limit": CANARY_SETTLEMENT_LIMIT,
        "environment": environment,
    }
    return hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()


# Domain separation and version of the receipt MAC.  The retired scheme (a
# client-computed HMAC over the bare payload digest, no context) counts as
# version 1 and never verifies here.
OPERATION_RECEIPT_SIGNATURE_CONTEXT: Final = "neurostack.billing.operation_receipt.v2"


def signs_rollout_revision(*, scope: str, to_state: str) -> bool:
    """Whether a receipt is bound to the rollout revision it was signed at.

    Opening rollout moves (``-> canary_allowlist`` / ``-> general``) sign the
    environment's ``rollout_revision`` read when the receipt is recorded: the
    transition's expected (``from``) revision.  Verification uses the stored
    revision at consumption, so a quorum is only valid while the state it was
    signed for still exists and can never be queued across a state change.
    Everything else signs ``null``: a move to ``disabled`` (rollback or
    incident) only reduces exposure and deliberately keeps an older unspent
    quorum usable, and top-up operations do not spend a rollout revision
    (they are fenced by ``topup_revision``).
    """

    return scope == "rollout" and to_state != "disabled"


def operation_receipt_message(
    receipt: BillingOperationReceipt, *, rollout_revision: int | None
) -> bytes:
    """The exact bytes the backend MACs for one stored receipt.

    Encoding: one JSON object, keys sorted, separators ``,`` and ``:`` (no
    whitespace), ASCII only; UUIDs as lowercase hyphenated text;
    ``expires_at`` as ``datetime.isoformat()`` of the UTC instant (for example
    ``2026-10-17T12:00:00+00:00``); absent values as ``null``.  Keys:

    * ``context``: :data:`OPERATION_RECEIPT_SIGNATURE_CONTEXT`;
    * the shared operation material: ``scope``, ``operation_id``,
      ``environment``, ``from_state``, ``to_state``, ``budget_digest``,
      ``deployment_digest``, ``expires_at``;
    * ``rollout_revision``: see :func:`signs_rollout_revision`;
    * this receipt's identity: ``role``, ``actor_user_id``,
      ``assignment_id``, ``nonce``;
    * the typed-evidence columns: ``evidence_type``, ``request_id``,
      ``inbox_high_watermark``, ``billing_fact_schema_digest``.
    """

    expires_at = receipt.expires_at
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise BillingOperationRejected("billing_operation_expiry_invalid")
    canonical = {
        "actor_user_id": _uuid_text(receipt.actor_user_id),
        "assignment_id": _uuid_text(receipt.assignment_id),
        "billing_fact_schema_digest": receipt.billing_fact_schema_digest,
        "budget_digest": receipt.budget_digest,
        "context": OPERATION_RECEIPT_SIGNATURE_CONTEXT,
        "deployment_digest": receipt.deployment_digest,
        "environment": receipt.environment,
        "evidence_type": receipt.evidence_type,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "from_state": receipt.from_state,
        "inbox_high_watermark": receipt.inbox_high_watermark,
        "nonce": _uuid_text(receipt.nonce),
        "operation_id": _uuid_text(receipt.operation_id),
        "request_id": (
            None if receipt.request_id is None else _uuid_text(receipt.request_id)
        ),
        "role": receipt.role,
        "rollout_revision": rollout_revision,
        "scope": receipt.scope,
        "to_state": receipt.to_state,
    }
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def operation_receipt_signature(
    receipt: BillingOperationReceipt,
    *,
    rollout_revision: int | None,
    signing_key: bytes,
) -> str:
    """Backend-only HMAC-SHA256; no caller-supplied signature is ever used."""

    if not signing_key:
        raise BillingOperationRejected("billing_operation_signing_unavailable")
    return hmac.new(
        signing_key,
        operation_receipt_message(receipt, rollout_revision=rollout_revision),
        hashlib.sha256,
    ).hexdigest()


def verify_operation_receipt_signature(
    receipt: BillingOperationReceipt,
    *,
    rollout_revision: int | None,
    signing_key: bytes,
) -> None:
    """Recompute the stored row's signature; constant-time, fail closed."""

    expected = operation_receipt_signature(
        receipt, rollout_revision=rollout_revision, signing_key=signing_key
    )
    stored = receipt.signature_sha256
    if not is_sha256_digest(stored) or not hmac.compare_digest(expected, stored):
        raise BillingOperationRejected("billing_operation_signature_invalid")


@dataclass(frozen=True, slots=True)
class OperatorReceipt:
    """One already-authenticated receipt used in a three-person quorum."""

    id: UUID
    assignment_id: UUID
    operation_id: UUID
    scope: OperationScope
    role: OperatorRole
    actor_user_id: UUID
    environment: str
    nonce: UUID
    payload_digest: str
    deployment_digest: str
    budget_digest: str
    from_state: str
    to_state: str
    expires_at: datetime
    applied_at: datetime | None = None


def operator_receipt(row: BillingOperationReceipt) -> OperatorReceipt:
    """Project one persisted receipt onto the pure quorum-check shape."""

    return OperatorReceipt(
        id=row.id,
        assignment_id=row.assignment_id,
        operation_id=row.operation_id,
        scope=cast(OperationScope, row.scope),
        role=cast(OperatorRole, row.role),
        actor_user_id=row.actor_user_id,
        environment=row.environment,
        nonce=row.nonce,
        payload_digest=row.payload_digest,
        deployment_digest=row.deployment_digest,
        budget_digest=row.budget_digest,
        from_state=row.from_state,
        to_state=row.to_state,
        expires_at=row.expires_at,
        applied_at=row.applied_at,
    )


def require_operator_quorum(
    receipts: Sequence[OperatorReceipt],
    *,
    scope: OperationScope,
    operation_id: UUID,
    environment: str,
    payload_digest: str,
    material: "OperationMaterial | None" = None,
    now: datetime | None = None,
) -> None:
    """Require exactly deployer/approver/reviewer, each a distinct actor.

    The caller locks receipt rows before invoking this pure check.  The
    function intentionally refuses extras, reused receipts, differing
    operation material, expired evidence, and an actor holding more than one
    duty; those are all ambiguous authorization states.
    """

    current = now or datetime.now(timezone.utc)
    if len(receipts) != 3:
        raise BillingOperationRejected("billing_operation_quorum_incomplete")
    roles = {receipt.role for receipt in receipts}
    if roles != OPERATOR_ROLES:
        raise BillingOperationRejected("billing_operation_roles_invalid")
    if len({receipt.actor_user_id for receipt in receipts}) != 3:
        raise BillingOperationRejected("billing_operation_actor_conflict")
    if len({receipt.nonce for receipt in receipts}) != 3:
        raise BillingOperationRejected("billing_operation_nonce_conflict")
    for receipt in receipts:
        if (
            receipt.scope != scope
            or receipt.operation_id != operation_id
            or receipt.environment != environment
            or receipt.payload_digest != payload_digest
            or receipt.applied_at is not None
            or receipt.expires_at.tzinfo is None
            or receipt.expires_at <= current
            or (
                material is not None
                and (
                    receipt.deployment_digest != material.deployment_digest
                    or receipt.budget_digest != material.budget_digest
                    or receipt.from_state != material.from_state
                    or receipt.to_state != material.to_state
                    or receipt.expires_at != material.expires_at
                )
            )
        ):
            raise BillingOperationRejected("billing_operation_receipt_invalid")


def require_quorum_after(
    receipts: Iterable[BillingOperationReceipt], anchor_created_at: datetime
) -> None:
    """Refuse a quorum any of whose receipts is not strictly newer than *anchor*.

    *anchor_created_at* is the ``created_at`` of the rollout receipt of the
    current state.  Both timestamps are PostgreSQL ``now()``, which is the
    START of the inserting transaction, on one clock: fresh means the
    receipt's transaction started after the anchor's transaction started (not
    after it committed), and equal is stale.  A receipt that started in that
    window but read the previous state is caught by its signed rollout
    revision instead (:func:`signs_rollout_revision`).  This is the single
    freshness rule; it runs inside :meth:`BillingOperationAuthority.
    verified_quorum` over exactly the rows that are then validated and spent.
    """

    if any(receipt.created_at <= anchor_created_at for receipt in receipts):
        raise BillingOperationRejected("billing_rollout_quorum_stale")


@dataclass(frozen=True, slots=True)
class OperationMaterial:
    """The common, closed values that every operator signs for one action."""

    scope: OperationScope
    operation_id: UUID
    environment: str
    from_state: str
    to_state: str
    budget_digest: str
    deployment_digest: str
    expires_at: datetime

    @property
    def payload_digest(self) -> str:
        return operation_payload_digest(
            scope=self.scope,
            operation_id=self.operation_id,
            environment=self.environment,
            from_state=self.from_state,
            to_state=self.to_state,
            budget_digest=self.budget_digest,
            deployment_digest=self.deployment_digest,
            expires_at=self.expires_at,
        )


class BillingOperationAuthority:
    """Persist, sign and consume operator evidence without committing.

    Every caller owns its transaction.  A rollout/capability service invokes
    ``consume_quorum`` while holding this environment lock, then performs its
    protected state transition under the same PostgreSQL advisory lock.  The
    signing key is held only here and never serialized.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        signing_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._signing_key = signing_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def assign(
        self,
        *,
        material: OperationMaterial,
        role: OperatorRole,
        actor_user_id: UUID,
    ) -> BillingOperationAssignment:
        """Create one assignment; database uniqueness enforces separation."""

        if role not in OPERATOR_ROLES:
            raise BillingOperationRejected("billing_operation_role_invalid")
        require_receipt_lifetime(material.expires_at, now=self._clock())
        assignment = BillingOperationAssignment(
            operation_id=material.operation_id,
            environment=material.environment,
            scope=material.scope,
            role=role,
            actor_user_id=actor_user_id,
            payload_digest=material.payload_digest,
            expires_at=material.expires_at,
            active=True,
        )
        self._session.add(assignment)
        await self._session.flush()
        return assignment

    async def _signed_rollout_revision(
        self, *, scope: str, environment: str, to_state: str
    ) -> int | None:
        """The stored rollout revision a receipt is (to be) signed at, if any."""

        if not signs_rollout_revision(scope=scope, to_state=to_state):
            return None
        revision = await self._session.scalar(
            select(BillingRolloutControl.rollout_revision).where(
                BillingRolloutControl.environment == environment
            )
        )
        if revision is None:
            raise BillingOperationRejected("billing_operation_rollout_unavailable")
        return int(revision)

    async def record_receipt(
        self,
        *,
        assignment_id: UUID,
        actor_user_id: UUID,
        nonce: UUID,
        material: OperationMaterial,
        evidence_type: str | None = None,
        request_id: UUID | None = None,
        inbox_high_watermark: int | None = None,
    ) -> BillingOperationReceipt:
        """Append one single-use receipt, signed here, for its assigned actor.

        ``actor_user_id`` is the authenticated JWT subject.  The receipt is
        refused unless it is that actor's own active, unconsumed assignment
        for exactly this material and the nonce was never used; only then
        does the backend compute and store the signature.
        """

        if not self._signing_key:
            raise BillingOperationRejected("billing_operation_signing_unavailable")
        if evidence_type not in {
            None,
            "producer",
            "credit",
            "reconciliation",
            "verification",
        }:
            raise BillingOperationRejected("billing_operation_evidence_type_invalid")
        if inbox_high_watermark is not None and inbox_high_watermark < 0:
            raise BillingOperationRejected("billing_operation_watermark_invalid")
        fact_schema_digest: str | None = None
        if evidence_type is not None:
            # The database/service boundary derives this from the deployed
            # contract.  A caller can provide the role payload digest, but it
            # cannot choose which fact schema the receipt claims to attest.
            from src.services.billing_outbox import (
                authoritative_billing_fact_schema_digest,
            )

            try:
                fact_schema_digest = authoritative_billing_fact_schema_digest(
                    material.environment
                )
            except Exception as exc:
                raise BillingOperationRejected(
                    "billing_operation_fact_schema_unavailable"
                ) from exc
        require_receipt_lifetime(material.expires_at, now=self._clock())
        result = await self._session.execute(
            select(BillingOperationAssignment)
            .where(BillingOperationAssignment.id == assignment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assignment = result.scalar_one_or_none()
        if (
            assignment is None
            or not assignment.active
            or assignment.consumed_at is not None
            or assignment.actor_user_id != actor_user_id
            or assignment.operation_id != material.operation_id
            or assignment.environment != material.environment
            or assignment.scope != material.scope
            or assignment.payload_digest != material.payload_digest
            or assignment.expires_at != material.expires_at
        ):
            raise BillingOperationRejected("billing_operation_assignment_invalid")
        # The unique constraint stays the race-proof backstop; this gives the
        # ordinary reuse a typed refusal before anything is signed.
        nonce_used = await self._session.scalar(
            select(BillingOperationReceipt.id)
            .where(BillingOperationReceipt.nonce == nonce)
            .limit(1)
        )
        if nonce_used is not None:
            raise BillingOperationRejected("billing_operation_nonce_conflict")
        rollout_revision = await self._signed_rollout_revision(
            scope=material.scope,
            environment=material.environment,
            to_state=material.to_state,
        )
        receipt = BillingOperationReceipt(
            assignment_id=assignment.id,
            operation_id=material.operation_id,
            environment=material.environment,
            scope=material.scope,
            role=assignment.role,
            actor_user_id=actor_user_id,
            nonce=nonce,
            payload_digest=material.payload_digest,
            deployment_digest=material.deployment_digest,
            budget_digest=material.budget_digest,
            from_state=material.from_state,
            to_state=material.to_state,
            evidence_type=evidence_type,
            request_id=request_id,
            inbox_high_watermark=inbox_high_watermark,
            billing_fact_schema_digest=fact_schema_digest,
            expires_at=material.expires_at,
        )
        receipt.signature_sha256 = operation_receipt_signature(
            receipt, rollout_revision=rollout_revision, signing_key=self._signing_key
        )
        self._session.add(receipt)
        await self._session.flush()
        BILLING_OPERATION_RECEIPTS_TOTAL.labels(
            role=assignment.role, outcome="accepted"
        ).inc()
        return receipt

    async def revoke_assignment(self, *, assignment_id: UUID) -> None:
        """Revoke an unused assignment without rewriting its audit history."""

        result = await self._session.execute(
            select(BillingOperationAssignment)
            .where(BillingOperationAssignment.id == assignment_id)
            .with_for_update()
        )
        assignment = result.scalar_one_or_none()
        if assignment is None:
            raise BillingOperationRejected("billing_operation_assignment_invalid")
        if assignment.consumed_at is not None:
            raise BillingOperationRejected("billing_operation_assignment_consumed")
        assignment.active = False
        await self._session.flush()

    async def verified_quorum(
        self,
        *,
        material: OperationMaterial,
        fresh_after: datetime | None = None,
    ) -> Sequence[BillingOperationReceipt]:
        """Lock and authenticate the exact quorum rows of *material*; no spend.

        The one loader behind readiness (which must not spend the quorum) and
        :meth:`consume_quorum`, so both judge the same rows.  Order:
        freshness against *fresh_after* when given (:func:`require_quorum_after`),
        then the pure quorum shape, then every stored backend signature
        recomputed from its row and the rollout revision it must have been
        signed at.  A caller whose material signs the rollout revision holds
        the rollout control row lock, so that revision cannot move here.
        """

        if not self._signing_key:
            raise BillingOperationRejected("billing_operation_signing_unavailable")
        result = await self._session.execute(
            select(BillingOperationReceipt)
            .where(
                BillingOperationReceipt.operation_id == material.operation_id,
                BillingOperationReceipt.environment == material.environment,
                BillingOperationReceipt.scope == material.scope,
                BillingOperationReceipt.payload_digest == material.payload_digest,
                # Typed capability attestations share the operation identity
                # but are not part of the three-person quorum itself.
                BillingOperationReceipt.evidence_type.is_(None),
            )
            .order_by(BillingOperationReceipt.role)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = result.scalars().all()
        if fresh_after is not None:
            require_quorum_after(rows, fresh_after)
        require_operator_quorum(
            tuple(operator_receipt(row) for row in rows),
            scope=material.scope,
            operation_id=material.operation_id,
            environment=material.environment,
            payload_digest=material.payload_digest,
            material=material,
            now=self._clock(),
        )
        rollout_revision = await self._signed_rollout_revision(
            scope=material.scope,
            environment=material.environment,
            to_state=material.to_state,
        )
        for row in rows:
            verify_operation_receipt_signature(
                row, rollout_revision=rollout_revision, signing_key=self._signing_key
            )
        return rows

    async def consume_quorum(
        self,
        *,
        material: OperationMaterial,
        fresh_after: datetime | None = None,
    ) -> tuple[OperatorReceipt, OperatorReceipt, OperatorReceipt]:
        """Lock, authenticate, and consume the exact three receipts once."""

        if self._session.get_bind().dialect.name == "postgresql":
            await self._session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        billing_advisory_key(material.environment)
                    )
                )
            )
        rows = await self.verified_quorum(material=material, fresh_after=fresh_after)
        receipts = tuple(operator_receipt(row) for row in rows)
        applied_at = self._clock()
        for row in rows:
            row.applied_at = applied_at
        await self._session.flush()
        for receipt in receipts:
            BILLING_OPERATION_RECEIPTS_TOTAL.labels(
                role=receipt.role, outcome="consumed"
            ).inc()
        if len(receipts) != 3:  # Narrow tuple shape for strict callers.
            raise BillingOperationRejected("billing_operation_quorum_incomplete")
        return receipts[0], receipts[1], receipts[2]
