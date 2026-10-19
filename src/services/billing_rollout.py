"""Fail-closed persisted rollout guard and signed rollout control plane.

Operator invariants of the control plane (``docs/runbooks`` has no
deployment/rollback runbook in this tree; the Group 6 port owns it, and this
docstring is the reference until then):

* Opening quorums (``disabled -> canary_allowlist`` and
  ``canary_allowlist -> general``) are signed only AFTER the state they leave
  exists, and are never prepared ahead or queued across a state change.  Each
  such receipt signs the rollout revision current when it is recorded and
  must come from a transaction that started after the one that wrote the
  current state's rollout receipt.  Any transition in between (an
  incident-disable, a rollback, a re-canary) makes the whole quorum unusable
  (``billing_rollout_quorum_stale``); sign it again.  Only a move to
  ``disabled`` may spend an older quorum.
* A canary quorum signs its builder: ``budget_digest`` must equal
  ``canary_budget_digest(environment, builder)`` of the builder the
  transition opens.
* Every assignment and receipt expires at most
  ``BILLING_OPERATION_RECEIPT_MAX_TTL_SECONDS`` (default 24 h, at most 7 days)
  after it is created; readiness rows cannot outlive the receipts they name.
* Readiness rows are recorded by the bound duty: ``readiness`` by the
  deployer, ``backlog``/``provisioning``/``smoke`` by the reviewer.
* NOT IMPLEMENTED: ``deployment_digest`` (and readiness ``app_digest``) is
  compared only with what the operators signed, never with the build that is
  actually running.  Binding it to the running build needs deploy-time
  injection; it is a production precondition owned by the Group 9 D3 wiring.
  Until then a quorum can attest a digest that the process does not run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Final, Sequence
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.billing import (
    BillingCheckoutSession,
    BillingOperationReceipt,
    BillingRolloutControl,
    BillingRolloutReadinessReceipt,
    BillingRolloutReceipt,
    BillingRosterControl,
)
from src.repositories.billing import (
    BillingRepository,
    BillingTransitionEvidence,
    billing_roster_advisory_key,
)
from src.services.billing_operations import (
    BillingOperationAuthority,
    BillingOperationRejected,
    OperationMaterial,
    canary_budget_digest,
)
from src.services.billing_outbox import billing_fact_bundle_digest

BILLING_CHECKOUT_UNAVAILABLE = "billing_checkout_unavailable"

# Which duty records each readiness kind (the JWT subject must be that
# receipt's actor).  A row names the deployer's operation receipt
# (``operation_receipt_id``) and the reviewer's (``provider_receipt_id``):
# the deployer attests the deployed application's own readiness; the
# reviewer attests the three provider-side checks, i.e. the ASAAS webhook
# provisioning readback and smoke (``AsaasWebhookProvisioner.readback`` /
# ``.smoke``) and the delivery backlog (``reconcile_backlog``).  An approver
# or an actor outside the quorum records nothing.
_READINESS_RECORDER_ROLE: Final = {
    "readiness": "deployer",
    "backlog": "reviewer",
    "provisioning": "reviewer",
    "smoke": "reviewer",
}


def _is_digest(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _digest_file(path: Path) -> str:
    try:
        metadata = path.lstat()
        if not path.is_file() or path.is_symlink() or metadata.st_nlink != 1:
            raise OSError("readiness file is not a private regular file")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise BillingOperationRejected(
            "billing_rollout_readiness_evidence_unavailable"
        ) from exc


def _readiness_content_digest(
    *,
    environment: str,
    receipt_kind: str,
    rollout_revision: int,
    roster_revision: int,
    operation_id: UUID,
    operation_receipt_id: UUID,
    provider_receipt_id: UUID,
    rule_digest: str,
    bundle_digest: str,
    app_digest: str,
) -> str:
    payload = {
        "app_digest": app_digest,
        "bundle_digest": bundle_digest,
        "environment": environment,
        "operation_id": str(operation_id),
        "operation_receipt_id": str(operation_receipt_id),
        "provider_receipt_id": str(provider_receipt_id),
        "receipt_kind": receipt_kind,
        "rollout_revision": rollout_revision,
        "roster_revision": roster_revision,
        "rule_digest": rule_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _readiness_evidence_is_bound(
    row: BillingRolloutReadinessReceipt,
    evidence_by_id: dict[UUID, BillingOperationReceipt],
    *,
    operation_id: UUID,
    payload_digest: str,
    deployment_digest: str,
    rule_digest: str,
    bundle_digest: str,
    now: datetime,
    current_roster_revision: int | None = None,
) -> bool:
    deployer = evidence_by_id.get(row.operation_receipt_id)
    provider = evidence_by_id.get(row.provider_receipt_id)
    return bool(
        deployer is not None
        and provider is not None
        and row.rule_digest == rule_digest
        and row.bundle_digest == bundle_digest
        and row.app_digest == deployment_digest
        and (
            current_roster_revision is None
            or row.roster_revision == current_roster_revision
        )
        and deployer.deployment_digest == deployment_digest
        and provider.deployment_digest == deployment_digest
        and row.operation_id == operation_id
        and row.operation_id == deployer.operation_id == provider.operation_id
        and deployer.payload_digest == payload_digest
        and provider.payload_digest == payload_digest
        and deployer.from_state == provider.from_state
        and deployer.to_state == provider.to_state
        and deployer.actor_user_id != provider.actor_user_id
        and deployer.environment == row.environment
        and provider.environment == row.environment
        and deployer.scope == "rollout"
        and provider.scope == "rollout"
        and deployer.role == "deployer"
        and provider.role == "reviewer"
        # Readiness references the quorum the ``general`` transition is about
        # to spend; spent evidence was already used by a transition.
        and deployer.applied_at is None
        and provider.applied_at is None
        and row.observed_at <= now < row.expires_at
        and deployer.created_at <= row.observed_at
        and provider.created_at <= row.observed_at
        and deployer.expires_at > now
        and provider.expires_at > now
        and deployer.expires_at >= row.expires_at
        and provider.expires_at >= row.expires_at
        and row.content_sha256
        == _readiness_content_digest(
            environment=row.environment,
            receipt_kind=row.receipt_kind,
            rollout_revision=row.rollout_revision,
            roster_revision=row.roster_revision,
            operation_id=row.operation_id,
            operation_receipt_id=row.operation_receipt_id,
            provider_receipt_id=row.provider_receipt_id,
            rule_digest=row.rule_digest,
            bundle_digest=row.bundle_digest,
            app_digest=row.app_digest,
        )
    )


async def _current_state_anchor(
    session: AsyncSession, control: BillingRolloutControl
) -> datetime | None:
    """``created_at`` of the rollout receipt that produced the current state.

    An approval is for the next transition of the state that exists when it is
    signed.  Without a freshness anchor, a quorum signed before an
    incident-disable (and a re-canary) would still promote afterwards.  Both
    readiness and every opening transition pass this anchor to
    :meth:`BillingOperationAuthority.verified_quorum`, whose
    :func:`require_quorum_after` judges exactly the rows it validates (and
    ``consume_quorum`` spends): each must come from a transaction that started
    strictly after the anchor's transaction started.  A never-transitioned
    environment has no current receipt and nothing to compare against
    (``None``); a dangling pointer is refused as stale.
    """

    if control.rollout_receipt_id is None:
        return None
    anchor = await session.scalar(
        select(BillingRolloutReceipt.created_at).where(
            BillingRolloutReceipt.id == control.rollout_receipt_id
        )
    )
    if anchor is None:
        raise BillingOperationRejected("billing_rollout_quorum_stale")
    return anchor


class BillingCheckoutUnavailable(RuntimeError):
    """Raised when persisted rollout does not authorize this checkout."""


class BillingTopupUnavailable(BillingCheckoutUnavailable):
    """Raised when rollout/capability does not authorize a top-up."""


async def require_billing_checkout_rollout(
    db: AsyncSession,
    builder_id: UUID,
) -> None:
    """Authorize from the canonical persisted row, denying every uncertainty.

    Group 1 creates and bootstraps this table as ``disabled``.  Until that
    migration exists, the query fails and this guard intentionally denies.
    No environment variable, header, or request field can replace the row.
    """

    if not settings.is_asaas_configuration_compatible:
        raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE)

    try:
        result = await db.execute(
            text("""
                SELECT state, canary_builder_id
                FROM billing_rollout_control
                WHERE environment = :environment
                LIMIT 1
                """),
            {"environment": settings.environment},
        )
        row = result.mappings().one_or_none()
    except Exception as exc:
        raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE) from exc

    if row is None:
        raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE)
    state = row.get("state")
    canary_builder_id = row.get("canary_builder_id")
    if state == "general":
        return
    if state == "canary_allowlist" and str(canary_builder_id) == str(builder_id):
        return
    raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE)


async def require_billing_topup_rollout(
    db: AsyncSession,
    *,
    builder_id: UUID,
    topup_order_id: UUID,
    checkout_session_id: UUID,
    attempt_no: int = 1,
) -> BillingCheckoutSession:
    """Atomically authorize and reserve a top-up before provider egress.

    The caller owns commit/rollback and must commit this reservation before it
    invokes ASAAS.  This is deliberately one operation; callers must not split
    rollout reads from session creation.
    """

    if not settings.is_asaas_configuration_compatible:
        raise BillingTopupUnavailable(BILLING_CHECKOUT_UNAVAILABLE)
    try:
        return await BillingRepository(db).authorize_and_reserve_topup_checkout(
            environment=settings.environment,
            builder_id=builder_id,
            topup_order_id=topup_order_id,
            checkout_session_id=checkout_session_id,
            attempt_no=attempt_no,
        )
    except Exception as exc:
        raise BillingTopupUnavailable(BILLING_CHECKOUT_UNAVAILABLE) from exc


async def transition_billing_rollout(
    db: AsyncSession,
    *,
    expected_revision: int,
    to_state: str,
    evidence: BillingTransitionEvidence,
    canary_builder_id: UUID | None = None,
) -> BillingRolloutControl:
    """Transition the current environment without committing the caller's unit."""

    return await BillingRepository(db).transition_rollout(
        environment=settings.environment,
        expected_revision=expected_revision,
        to_state=to_state,
        evidence=evidence,
        canary_builder_id=canary_builder_id,
    )


async def set_billing_topup_gate(
    db: AsyncSession,
    *,
    expected_revision: int,
    enabled: bool,
    evidence: BillingTransitionEvidence,
    capability_receipt_id: UUID | None = None,
    disable_reason: str | None = None,
) -> BillingRolloutControl:
    """Legacy repository facade; it cannot bypass the G5 capability fence.

    ``enabled=True`` requires a one-use binding, which is created only by
    :class:`BillingTopupCapabilityService`.  ``enabled=False`` requires an
    explicit health/revocation reason; public callers use that same service.
    """

    return await BillingRepository(db).set_topup_commercial_enabled(
        environment=settings.environment,
        expected_revision=expected_revision,
        enabled=enabled,
        evidence=evidence,
        capability_receipt_id=capability_receipt_id,
        disable_reason=disable_reason,
    )


class BillingRolloutOperationService:
    """Signed administrative rollout and incident transitions.

    This is intentionally separate from checkout authorization.  It turns a
    validated, three-person local receipt bundle into one repository
    transition; it never provisions or contacts ASAAS.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        signing_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = BillingRepository(session)
        self._authority = BillingOperationAuthority(
            session, signing_key=signing_key, clock=clock
        )

    async def record_readiness_receipt(
        self,
        *,
        environment: str,
        receipt_kind: str,
        rollout_revision: int,
        roster_revision: int,
        status: str,
        content_sha256: str,
        material: OperationMaterial,
        operation_receipt_id: UUID,
        provider_receipt_id: UUID,
        rule_digest: str,
        bundle_digest: str,
        app_digest: str,
        observed_at: datetime,
        expires_at: datetime,
        recorder_user_id: UUID,
    ) -> BillingRolloutReadinessReceipt:
        """Append readiness bound to the unspent quorum ``general`` will spend.

        The HTTP body is a request for evidence, not evidence itself.  The
        service checks the current rollout row, the exact on-disk rule and
        fact-bundle digests, and the deployer/reviewer receipts of one
        complete, unspent ``canary_allowlist -> general`` quorum before it
        writes the append-only readiness row.  Recording does not consume the
        quorum: the ``general`` transition spends it exactly once.  Every row
        of one rollout revision must name the same receipt pair, and a pair
        used at another revision is refused, so no row can be appended that
        the transition could not use.  *recorder_user_id* (the JWT subject)
        must be the actor of the bound receipt whose duty owns the kind
        (``_READINESS_RECORDER_ROLE``); the table has no recorder column, so
        this is enforced here, at insert.
        """

        now = self._authority._clock()
        if (
            material.scope != "rollout"
            or material.environment != environment
            or status != "passed"
            or receipt_kind not in {"readiness", "backlog", "provisioning", "smoke"}
        ):
            raise BillingOperationRejected("billing_rollout_readiness_invalid")
        if material.from_state != "canary_allowlist" or material.to_state != "general":
            raise BillingOperationRejected(
                "billing_rollout_readiness_transition_invalid"
            )
        if observed_at.tzinfo is None or expires_at.tzinfo is None:
            raise BillingOperationRejected("billing_rollout_readiness_invalid")
        if observed_at > now or expires_at <= observed_at or expires_at <= now:
            raise BillingOperationRejected("billing_rollout_readiness_expired")
        if environment not in {"development", "staging", "production", "test"}:
            raise BillingOperationRejected("billing_rollout_readiness_invalid")
        if not _is_digest(content_sha256):
            raise BillingOperationRejected("billing_rollout_readiness_invalid")
        exact_rule_digest = _digest_file(Path("config/prometheus_rules.yml"))
        exact_bundle_digest = billing_fact_bundle_digest()
        if (
            not _is_digest(rule_digest)
            or not _is_digest(bundle_digest)
            or not _is_digest(app_digest)
            or rule_digest != exact_rule_digest
            or bundle_digest != exact_bundle_digest
            or app_digest != material.deployment_digest
        ):
            raise BillingOperationRejected("billing_rollout_readiness_evidence_invalid")
        if self._repository.session.get_bind().dialect.name == "postgresql":
            await self._repository.session.execute(
                select(func.pg_advisory_xact_lock(billing_roster_advisory_key()))
            )
        roster = await self._repository.session.scalar(
            select(BillingRosterControl).with_for_update()
        )
        if roster is None or roster.billing_roster_revision != roster_revision:
            raise BillingOperationRejected("billing_rollout_readiness_roster_invalid")
        control = await self._repository.session.scalar(
            select(BillingRolloutControl)
            .where(BillingRolloutControl.environment == environment)
            .with_for_update()
        )
        if control is None or control.rollout_revision != rollout_revision:
            raise BillingOperationRejected("billing_rollout_readiness_revision_invalid")
        if control.state != material.from_state:
            raise BillingOperationRejected("billing_rollout_from_state_mismatch")
        if operation_receipt_id == provider_receipt_id:
            raise BillingOperationRejected("billing_rollout_readiness_receipt_invalid")
        # The exact rows ``consume_quorum`` will spend for this material,
        # judged by the same loader: fresh, a complete quorum, backend-signed.
        quorum_rows = await self._authority.verified_quorum(
            material=material,
            fresh_after=await _current_state_anchor(self._repository.session, control),
        )
        operation_rows = {row.id: row for row in quorum_rows}
        deployer = operation_rows.get(operation_receipt_id)
        provider = operation_rows.get(provider_receipt_id)
        if (
            deployer is None
            or provider is None
            or deployer.role != "deployer"
            or provider.role != "reviewer"
            or deployer.deployment_digest != material.deployment_digest
            or provider.deployment_digest != material.deployment_digest
            or deployer.budget_digest != material.budget_digest
            or provider.budget_digest != material.budget_digest
            or deployer.payload_digest != material.payload_digest
            or provider.payload_digest != material.payload_digest
            or deployer.from_state != material.from_state
            or provider.from_state != material.from_state
            or deployer.to_state != material.to_state
            or provider.to_state != material.to_state
            or deployer.actor_user_id == provider.actor_user_id
            or deployer.expires_at <= now
            or provider.expires_at <= now
            or deployer.created_at > observed_at
            or provider.created_at > observed_at
            or expires_at > deployer.expires_at
            or expires_at > provider.expires_at
        ):
            raise BillingOperationRejected("billing_rollout_readiness_receipt_invalid")
        recorder = (
            deployer
            if _READINESS_RECORDER_ROLE[receipt_kind] == "deployer"
            else provider
        )
        if recorder.actor_user_id != recorder_user_id:
            raise BillingOperationRejected("billing_rollout_readiness_recorder_invalid")
        expected_content = _readiness_content_digest(
            environment=environment,
            receipt_kind=receipt_kind,
            rollout_revision=rollout_revision,
            roster_revision=roster_revision,
            operation_id=material.operation_id,
            operation_receipt_id=operation_receipt_id,
            provider_receipt_id=provider_receipt_id,
            rule_digest=rule_digest,
            bundle_digest=bundle_digest,
            app_digest=app_digest,
        )
        if content_sha256 != expected_content:
            raise BillingOperationRejected("billing_rollout_readiness_digest_invalid")
        # Recovery: these refusals are fail-closed and can leave a revision
        # with no completable readiness set (one pair per revision; a pair
        # whose quorum gained a receipt or whose rows expired cannot finish,
        # and no other pair is accepted).  Recover by rolling back to
        # ``disabled``, re-opening the canary (a new revision) and signing a
        # fresh ``canary_allowlist -> general`` quorum.
        pair = (operation_receipt_id, provider_receipt_id)
        recorded = await self._repository.session.execute(
            select(BillingRolloutReadinessReceipt).where(
                BillingRolloutReadinessReceipt.environment == environment,
                or_(
                    BillingRolloutReadinessReceipt.rollout_revision == rollout_revision,
                    BillingRolloutReadinessReceipt.operation_receipt_id.in_(pair),
                    BillingRolloutReadinessReceipt.provider_receipt_id.in_(pair),
                ),
            )
        )
        for row in recorded.scalars():
            if row.rollout_revision != rollout_revision:
                raise BillingOperationRejected(
                    "billing_rollout_readiness_receipt_reused"
                )
            if (row.operation_receipt_id, row.provider_receipt_id) != pair:
                raise BillingOperationRejected(
                    "billing_rollout_readiness_operation_conflict"
                )
            if row.receipt_kind == receipt_kind:
                raise BillingOperationRejected("billing_rollout_readiness_duplicate")
        receipt = BillingRolloutReadinessReceipt(
            environment=environment,
            receipt_kind=receipt_kind,
            rollout_revision=rollout_revision,
            status="passed",
            content_sha256=content_sha256,
            operation_id=material.operation_id,
            operation_receipt_id=operation_receipt_id,
            provider_receipt_id=provider_receipt_id,
            roster_revision=roster_revision,
            rule_digest=rule_digest,
            bundle_digest=bundle_digest,
            app_digest=app_digest,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        self._repository.session.add(receipt)
        await self._repository.session.flush()
        return receipt

    async def transition(
        self,
        *,
        expected_revision: int,
        material: OperationMaterial,
        canary_builder_id: UUID | None = None,
    ) -> BillingRolloutControl:
        """Transition disabled/canary/general only from a quorum receipt.

        The signed ``from_state`` must be the persisted state.  ``general``
        additionally spends exactly the quorum its four readiness rows name.
        """

        if material.scope != "rollout" or material.to_state not in {
            "disabled",
            "canary_allowlist",
            "general",
        }:
            raise BillingOperationRejected("billing_rollout_transition_invalid")
        if material.to_state == "canary_allowlist" and (
            canary_builder_id is None
            or material.budget_digest
            != canary_budget_digest(
                environment=material.environment, canary_builder_id=canary_builder_id
            )
        ):
            # Every signer signed this builder (see ``canary_budget_digest``);
            # a caller holding the quorum cannot open another one's canary.
            raise BillingOperationRejected("billing_rollout_canary_budget_unbound")
        readiness_rows: Sequence[BillingRolloutReadinessReceipt] = ()
        if material.to_state == "general":
            now = self._authority._clock()
            kinds = {"readiness", "backlog", "provisioning", "smoke"}
            if self._repository.session.get_bind().dialect.name == "postgresql":
                await self._repository.session.execute(
                    select(func.pg_advisory_xact_lock(billing_roster_advisory_key()))
                )
            roster = await self._repository.session.scalar(
                select(BillingRosterControl).with_for_update()
            )
            if roster is None:
                raise BillingOperationRejected(
                    "billing_rollout_readiness_roster_invalid"
                )
            result = await self._repository.session.execute(
                select(BillingRolloutReadinessReceipt)
                .where(
                    BillingRolloutReadinessReceipt.environment == material.environment,
                    BillingRolloutReadinessReceipt.rollout_revision
                    == expected_revision,
                    BillingRolloutReadinessReceipt.operation_id
                    == material.operation_id,
                    BillingRolloutReadinessReceipt.status == "passed",
                    BillingRolloutReadinessReceipt.observed_at <= now,
                    BillingRolloutReadinessReceipt.expires_at > now,
                )
                .with_for_update()
            )
            readiness_rows = result.scalars().all()
            if (
                len(readiness_rows) != 4
                or {row.receipt_kind for row in readiness_rows} != kinds
            ):
                raise BillingOperationRejected("billing_rollout_readiness_incomplete")
            exact_rule_digest = _digest_file(Path("config/prometheus_rules.yml"))
            exact_bundle_digest = billing_fact_bundle_digest()
            evidence_ids = {
                identifier
                for row in readiness_rows
                for identifier in (row.operation_receipt_id, row.provider_receipt_id)
            }
            evidence_result = await self._repository.session.execute(
                select(BillingOperationReceipt).where(
                    BillingOperationReceipt.id.in_(evidence_ids),
                    BillingOperationReceipt.environment == material.environment,
                    BillingOperationReceipt.scope == "rollout",
                )
            )
            evidence_by_id = {row.id: row for row in evidence_result.scalars().all()}
            if any(
                not _readiness_evidence_is_bound(
                    row,
                    evidence_by_id,
                    operation_id=material.operation_id,
                    payload_digest=material.payload_digest,
                    deployment_digest=material.deployment_digest,
                    rule_digest=exact_rule_digest,
                    bundle_digest=exact_bundle_digest,
                    now=now,
                    current_roster_revision=roster.billing_roster_revision,
                )
                for row in readiness_rows
            ):
                raise BillingOperationRejected("billing_rollout_readiness_unbound")
        # After the roster lock (lock order), before anything is consumed.
        control = await self._repository.lock_rollout_control(
            environment=material.environment
        )
        if control.state != material.from_state:
            raise BillingOperationRejected("billing_rollout_from_state_mismatch")
        # Moving to ``disabled`` only reduces exposure, so an older unspent
        # rollback quorum stays usable; every opening move must be fresh.
        fresh_after = (
            None
            if material.to_state == "disabled"
            else await _current_state_anchor(self._repository.session, control)
        )
        receipts = await self._authority.consume_quorum(
            material=material, fresh_after=fresh_after
        )
        deployer = next(receipt for receipt in receipts if receipt.role == "deployer")
        reviewer = next(receipt for receipt in receipts if receipt.role == "reviewer")
        if any(
            (row.operation_receipt_id, row.provider_receipt_id)
            != (deployer.id, reviewer.id)
            for row in readiness_rows
        ):
            raise BillingOperationRejected("billing_rollout_readiness_unbound")
        return await self._repository.transition_rollout(
            environment=material.environment,
            expected_revision=expected_revision,
            to_state=material.to_state,
            evidence=BillingTransitionEvidence(
                nonce=deployer.nonce,
                deployment_digest=material.deployment_digest,
                actor_user_id=deployer.actor_user_id,
                expires_at=material.expires_at,
            ),
            canary_builder_id=canary_builder_id,
        )

    async def disable_for_incident(
        self,
        *,
        expected_revision: int,
        material: OperationMaterial,
    ) -> BillingRolloutControl:
        """An incident can only lock rollout down to ``disabled``."""

        if material.scope != "incident" or material.to_state != "disabled":
            raise BillingOperationRejected("billing_incident_transition_invalid")
        receipts = await self._authority.consume_quorum(material=material)
        deployer = next(receipt for receipt in receipts if receipt.role == "deployer")
        return await self._repository.transition_rollout(
            environment=material.environment,
            expected_revision=expected_revision,
            to_state="disabled",
            evidence=BillingTransitionEvidence(
                nonce=deployer.nonce,
                deployment_digest=material.deployment_digest,
                actor_user_id=deployer.actor_user_id,
                expires_at=material.expires_at,
            ),
            canary_builder_id=None,
        )
