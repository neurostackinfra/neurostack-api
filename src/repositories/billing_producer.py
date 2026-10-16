"""Transactional persistence for one-use grants and producer receipt pairs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.billing_producer import (
    BillingProducerActivePair,
    BillingProducerBuildAttestation,
    BillingProducerGrant,
    BillingProducerReceipt,
)
from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    CREATE_ACTION,
    DEPLOYED_TYPE,
    ENVIRONMENT,
    OPERATIONAL_ROLE,
    PREFLIGHT_ACTION,
    PREFLIGHT_GRANT_TTL,
    RECEIPT_TTL,
    SERVICE_ARN,
    VERIFIED_TYPE,
    VERIFY_ACTION,
    ProducerHandoffRejected,
    ProducerReadback,
    digest_json,
    fingerprint,
    preflight_scope,
    receipt_expiry,
    receipt_scope,
    sign_receipt,
    verify_grant_signature,
    verify_receipt_signature,
)


class BillingProducerReceiptRepository:
    """Operate inside the caller's transaction; no method commits independently."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_active_pair(
        self, *, environment: str = ENVIRONMENT, for_update: bool = False
    ) -> BillingProducerActivePair | None:
        query = select(BillingProducerActivePair).where(
            BillingProducerActivePair.environment == environment
        )
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        rows = (await self.session.execute(query)).scalars().all()
        if len(rows) > 1:
            raise ProducerHandoffRejected("active pair pointer is duplicated")
        return rows[0] if rows else None

    async def get_receipt(
        self,
        *,
        receipt_id: UUID,
        operation_id: UUID,
        receipt_type: str | None = None,
    ) -> BillingProducerReceipt | None:
        query = select(BillingProducerReceipt).where(
            BillingProducerReceipt.id == receipt_id,
            BillingProducerReceipt.operation_id == operation_id,
        )
        if receipt_type is not None:
            query = query.where(BillingProducerReceipt.receipt_type == receipt_type)
        return (await self.session.execute(query)).scalar_one_or_none()

    async def get_build_attestation(
        self, *, attestation_id: UUID, attestation_digest: str
    ) -> BillingProducerBuildAttestation | None:
        result = await self.session.execute(
            select(BillingProducerBuildAttestation).where(
                BillingProducerBuildAttestation.id == attestation_id,
                BillingProducerBuildAttestation.payload_digest == attestation_digest,
            )
        )
        return result.scalar_one_or_none()

    async def register_build_attestation(
        self,
        attestation: BillingProducerBuildAttestation,
        *,
        build_attestation_public_key: str,
    ) -> None:
        """Persist only signed immutable provenance pinned to an image digest."""

        payload = attestation.payload
        image_digest = payload.get("app_image_digest")
        artifact_reference = payload.get("immutable_artifact_reference")
        if (
            payload.get("attestation_type") != "billing_build_attestation.v1"
            or not isinstance(image_digest, str)
            or not isinstance(artifact_reference, str)
            or not artifact_reference.endswith(f"@{image_digest}")
        ):
            raise ProducerHandoffRejected(
                "build attestation is not pinned to an immutable image"
            )
        verify_receipt_signature(
            payload,
            attestation.payload_digest,
            attestation.signature,
            build_attestation_public_key,
        )
        existing = await self.get_build_attestation(
            attestation_id=attestation.id,
            attestation_digest=attestation.payload_digest,
        )
        if existing is not None:
            raise ProducerHandoffRejected("build attestation ID already exists")
        self.session.add(attestation)
        await self.session.flush([attestation])

    async def register_grant(
        self, grant: BillingProducerGrant, *, authorization_public_key: str
    ) -> None:
        """Persist only a correctly signed grant scoped to the fixed target."""

        if (
            grant.environment != ENVIRONMENT
            or grant.operational_role != OPERATIONAL_ROLE
            or grant.cluster_arn != CLUSTER_ARN
            or grant.service_arn != SERVICE_ARN
            or grant.grant_kind not in {"assignment", "authorization"}
            or grant.action not in {CREATE_ACTION, VERIFY_ACTION, PREFLIGHT_ACTION}
            or grant.revoked_at is not None
            or grant.consumed_at is not None
            or grant.expires_at <= grant.issued_at
            or (
                grant.action == PREFLIGHT_ACTION
                and (
                    grant.receipt_id is not None
                    or grant.verification_receipt_id is not None
                    or grant.expires_at - grant.issued_at > PREFLIGHT_GRANT_TTL
                )
            )
            or (
                grant.action != PREFLIGHT_ACTION
                and (grant.receipt_id is None or grant.verification_receipt_id is None)
            )
        ):
            raise ProducerHandoffRejected("grant is outside the producer allowlist")
        verify_grant_signature(grant, authorization_public_key)
        self.session.add(grant)
        await self.session.flush([grant])

    async def get_grants(
        self, grant_ids: tuple[UUID, UUID]
    ) -> tuple[BillingProducerGrant, BillingProducerGrant]:
        if grant_ids[0] == grant_ids[1]:
            raise ProducerHandoffRejected(
                "assignment and authorization IDs must differ"
            )
        result = await self.session.execute(
            select(BillingProducerGrant)
            .where(BillingProducerGrant.id.in_(grant_ids))
            .with_for_update()
        )
        grants = {grant.id: grant for grant in result.scalars().all()}
        if len(grants) != 2:
            raise ProducerHandoffRejected(
                "active assignment or authorization is unavailable"
            )
        return grants[grant_ids[0]], grants[grant_ids[1]]

    async def validate_and_consume_grants(
        self,
        *,
        assignment_id: UUID,
        authorization_id: UUID,
        actor_user_id: UUID,
        operation_id: UUID,
        action: str,
        receipt_id: UUID | None,
        verification_receipt_id: UUID | None,
        scope_digest: str,
        now: datetime,
        authorization_public_key: str,
    ) -> None:
        assignment, authorization = await self.get_grants(
            (assignment_id, authorization_id)
        )
        expected = (
            (assignment, "assignment"),
            (authorization, "authorization"),
        )
        for grant, kind in expected:
            if (
                grant.grant_kind != kind
                or grant.actor_user_id != actor_user_id
                or grant.operation_id != operation_id
                or grant.environment != ENVIRONMENT
                or grant.operational_role != OPERATIONAL_ROLE
                or grant.cluster_arn != CLUSTER_ARN
                or grant.service_arn != SERVICE_ARN
                or grant.action != action
                or grant.receipt_id != receipt_id
                or grant.verification_receipt_id != verification_receipt_id
                or grant.scope_digest != scope_digest
                or grant.issued_at > now
                or grant.expires_at <= now
                or grant.revoked_at is not None
                or grant.consumed_at is not None
                or (
                    action == PREFLIGHT_ACTION
                    and (
                        grant.receipt_id is not None
                        or grant.verification_receipt_id is not None
                        or grant.expires_at - grant.issued_at > PREFLIGHT_GRANT_TTL
                    )
                )
            ):
                raise ProducerHandoffRejected(
                    "assignment or authorization is inactive or outside exact scope"
                )
            verify_grant_signature(grant, authorization_public_key)
        assignment.consumed_at = now
        authorization.consumed_at = now
        await self.session.flush([assignment, authorization])

    async def save_deploy_receipt(
        self,
        *,
        receipt_id: UUID,
        operation_id: UUID,
        actor_user_id: UUID,
        verification_receipt_id: UUID,
        scope: Mapping[str, Any],
        now: datetime,
        receipt_signing_key: str,
    ) -> BillingProducerReceipt:
        if await self.get_receipt(receipt_id=receipt_id, operation_id=operation_id):
            raise ProducerHandoffRejected("receipt ID already exists")
        final_payload = {
            **dict(scope),
            "issued_at": now.isoformat(),
            "expires_at": receipt_expiry(now).isoformat(),
            "attestor_user_id": str(actor_user_id),
        }
        payload_digest, signature = sign_receipt(final_payload, receipt_signing_key)
        readback = final_payload["readback"]
        receipt = BillingProducerReceipt(
            id=receipt_id,
            operation_id=operation_id,
            receipt_type=DEPLOYED_TYPE,
            environment=ENVIRONMENT,
            operational_role=OPERATIONAL_ROLE,
            cluster_arn=CLUSTER_ARN,
            service_arn=SERVICE_ARN,
            deployment_id=readback["deployment_id"],
            task_set_id=readback["task_set_id"],
            config_generation=readback["config_generation"],
            compatibility_generation=readback["compatibility_generation"],
            app_digest=readback["app_image_digest"],
            bundle_digest=readback["bundle_digest"],
            readback_digest=final_payload["readback_digest"],
            build_attestation_id=UUID(final_payload["build_attestation_id"]),
            build_attestation_digest=final_payload["build_attestation_digest"],
            commit_sha=final_payload["commit_sha"],
            tree_sha=final_payload["tree_sha"],
            workflow_id=final_payload["workflow_id"],
            attestor_user_id=actor_user_id,
            verifier_user_id=None,
            supersedes_receipt_id=(
                UUID(final_payload["supersedes"]["receipt_id"])
                if final_payload["supersedes"]
                else None
            ),
            supersedes_receipt_digest=(
                final_payload["supersedes"]["receipt_digest"]
                if final_payload["supersedes"]
                else None
            ),
            supersedes_verification_receipt_id=(
                UUID(final_payload["supersedes"]["verification_receipt_id"])
                if final_payload["supersedes"]
                else None
            ),
            supersedes_verification_receipt_digest=(
                final_payload["supersedes"]["verification_receipt_digest"]
                if final_payload["supersedes"]
                else None
            ),
            issued_at=now,
            expires_at=now + RECEIPT_TTL,
            payload=final_payload,
            payload_digest=payload_digest,
            signature=signature,
        )
        self.session.add(receipt)
        await self.session.flush([receipt])
        return receipt

    async def validate_receipt(
        self, receipt: BillingProducerReceipt, receipt_public_key: str
    ) -> None:
        if receipt.expires_at <= datetime.now(timezone.utc):
            raise ProducerHandoffRejected("receipt has expired")
        await self.verify_receipt_signature_only(receipt, receipt_public_key)

    async def verify_receipt_signature_only(
        self, receipt: BillingProducerReceipt, receipt_public_key: str
    ) -> None:
        verify_receipt_signature(
            receipt.payload,
            receipt.payload_digest,
            receipt.signature,
            receipt_public_key,
        )

    async def activate_verified_pair(
        self,
        *,
        operation_id: UUID,
        deploy_receipt: BillingProducerReceipt,
        verification_receipt: BillingProducerReceipt,
        expected_current: Mapping[str, str] | None,
        deploy_public_key: str,
    ) -> BillingProducerActivePair:
        current = await self.get_active_pair(for_update=True)
        if expected_current is None:
            if current is not None:
                raise ProducerHandoffRejected(
                    "active pair changed before compare-and-swap"
                )
            pointer = BillingProducerActivePair(
                environment=ENVIRONMENT,
                operation_id=operation_id,
                receipt_id=deploy_receipt.id,
                receipt_digest=deploy_receipt.payload_digest,
                verification_receipt_id=verification_receipt.id,
                verification_receipt_digest=verification_receipt.payload_digest,
                deployment_fingerprint=fingerprint(deploy_receipt.payload),
                revision=1,
            )
            self.session.add(pointer)
        else:
            if current is None or any(
                (
                    str(current.receipt_id) != expected_current.get("receipt_id"),
                    current.receipt_digest != expected_current.get("receipt_digest"),
                    str(current.verification_receipt_id)
                    != expected_current.get("verification_receipt_id"),
                    current.verification_receipt_digest
                    != expected_current.get("verification_receipt_digest"),
                    current.operation_id != operation_id,
                )
            ):
                raise ProducerHandoffRejected(
                    "active pair changed before compare-and-swap"
                )
            old_deploy = await self.get_receipt(
                receipt_id=current.receipt_id,
                operation_id=operation_id,
                receipt_type=DEPLOYED_TYPE,
            )
            if old_deploy is None:
                raise ProducerHandoffRejected(
                    "superseded active receipt is unavailable"
                )
            old_verified = await self.get_receipt(
                receipt_id=current.verification_receipt_id,
                operation_id=operation_id,
                receipt_type=VERIFIED_TYPE,
            )
            if (
                old_deploy.payload_digest != current.receipt_digest
                or old_verified is None
                or old_verified.payload_digest != current.verification_receipt_digest
                or old_verified.payload.get("source_receipt_id") != str(old_deploy.id)
                or old_verified.payload.get("source_receipt_digest")
                != old_deploy.payload_digest
            ):
                raise ProducerHandoffRejected("superseded active pair is inconsistent")
            await self.verify_receipt_signature_only(old_deploy, deploy_public_key)
            await self.verify_receipt_signature_only(old_verified, deploy_public_key)
            if fingerprint(old_deploy.payload) != fingerprint(deploy_receipt.payload):
                raise ProducerHandoffRejected("runtime drift requires a new operation")
            current.receipt_id = deploy_receipt.id
            current.receipt_digest = deploy_receipt.payload_digest
            current.verification_receipt_id = verification_receipt.id
            current.verification_receipt_digest = verification_receipt.payload_digest
            current.deployment_fingerprint = fingerprint(deploy_receipt.payload)
            current.revision += 1
            current.updated_at = datetime.now(timezone.utc)
            pointer = current
        await self.session.flush([pointer])
        return pointer


class BillingProducerHandoffService:
    """Join signed grants, exact readbacks, append-only receipts, and CAS state."""

    def __init__(
        self,
        repository: BillingProducerReceiptRepository,
        *,
        authorization_public_key: str,
        receipt_signing_key: str,
        receipt_public_key: str,
        build_attestation_public_key: str,
    ):
        self.repository = repository
        self.authorization_public_key = authorization_public_key
        self.receipt_signing_key = receipt_signing_key
        self.receipt_public_key = receipt_public_key
        self.build_attestation_public_key = build_attestation_public_key

    async def preflight(
        self,
        *,
        actor_user_id: UUID,
        operation_id: UUID,
        assignment_id: UUID,
        authorization_id: UUID,
        readback: ProducerReadback,
        minimum_validity_seconds: int = 300,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if minimum_validity_seconds < 0 or minimum_validity_seconds > 300:
            raise ProducerHandoffRejected("invalid minimum validity window")
        if not all(
            (
                self.authorization_public_key,
                self.receipt_public_key,
                self.build_attestation_public_key,
            )
        ):
            raise ProducerHandoffRejected(
                "producer signing or provenance keys are unavailable"
            )
        now = now or datetime.now(timezone.utc)
        scope = preflight_scope(
            operation_id=operation_id,
            readback=readback,
            minimum_validity_seconds=minimum_validity_seconds,
        )
        await self.repository.validate_and_consume_grants(
            assignment_id=assignment_id,
            authorization_id=authorization_id,
            actor_user_id=actor_user_id,
            operation_id=operation_id,
            action=PREFLIGHT_ACTION,
            receipt_id=None,
            verification_receipt_id=None,
            scope_digest=digest_json(scope),
            now=now,
            authorization_public_key=self.authorization_public_key,
        )
        pointer = await self.repository.get_active_pair()
        if pointer is None:
            return {"action": "create", "current_active_pair": None}
        if pointer.operation_id != operation_id:
            raise ProducerHandoffRejected("another operation owns the active pair")
        deploy = await self.repository.get_receipt(
            receipt_id=pointer.receipt_id,
            operation_id=operation_id,
            receipt_type=DEPLOYED_TYPE,
        )
        verified = await self.repository.get_receipt(
            receipt_id=pointer.verification_receipt_id,
            operation_id=operation_id,
            receipt_type=VERIFIED_TYPE,
        )
        if deploy is None or verified is None:
            raise ProducerHandoffRejected("active pair is incomplete")
        await self.repository.verify_receipt_signature_only(
            deploy, self.receipt_public_key
        )
        await self.repository.verify_receipt_signature_only(
            verified, self.receipt_public_key
        )
        current_readback = readback.as_payload()
        attestation = await self.repository.get_build_attestation(
            attestation_id=deploy.build_attestation_id,
            attestation_digest=deploy.build_attestation_digest,
        )
        expected_verification = {
            "operation_id": str(operation_id),
            "receipt_type": VERIFIED_TYPE,
            "receipt_id": str(verified.id),
            "source_receipt_id": str(deploy.id),
            "source_receipt_digest": deploy.payload_digest,
            "environment": ENVIRONMENT,
            "operational_role": OPERATIONAL_ROLE,
            "cluster_arn": CLUSTER_ARN,
            "service_arn": SERVICE_ARN,
            "deployment_id": deploy.deployment_id,
            "task_set_id": deploy.task_set_id,
            "config_generation": deploy.config_generation,
            "compatibility_generation": deploy.compatibility_generation,
            "app_digest": deploy.app_digest,
            "bundle_digest": deploy.bundle_digest,
            "readback_digest": deploy.readback_digest,
            "build_attestation_id": str(deploy.build_attestation_id),
            "build_attestation_digest": deploy.build_attestation_digest,
            "commit_sha": deploy.commit_sha,
            "tree_sha": deploy.tree_sha,
            "workflow_id": deploy.workflow_id,
            "attestor_user_id": str(deploy.attestor_user_id),
            "verifier_user_id": str(verified.verifier_user_id),
        }
        if (
            deploy.attestor_user_id == verified.verifier_user_id
            or deploy.payload_digest != pointer.receipt_digest
            or verified.payload_digest != pointer.verification_receipt_digest
            or verified.payload.get("source_receipt_id") != str(deploy.id)
            or verified.payload.get("source_receipt_digest") != deploy.payload_digest
            or verified.payload.get("readback_digest") != deploy.readback_digest
            or any(
                verified.payload.get(field) != expected
                for field, expected in expected_verification.items()
            )
            or deploy.payload.get("operation_id") != str(operation_id)
            or deploy.payload.get("receipt_id") != str(deploy.id)
            or deploy.payload.get("verification_receipt_id") != str(verified.id)
            or deploy.payload.get("readback_digest") != deploy.readback_digest
            or digest_json(current_readback) != deploy.readback_digest
            or attestation is None
            or attestation.payload.get("app_image_digest") != deploy.app_digest
            or attestation.payload.get("bundle_digest") != deploy.bundle_digest
            or attestation.payload.get("commit_sha") != deploy.commit_sha
            or attestation.payload.get("tree_sha") != deploy.tree_sha
            or attestation.payload.get("workflow_id") != deploy.workflow_id
        ):
            raise ProducerHandoffRejected(
                "active pair does not match the current readback and provenance"
            )
        verify_receipt_signature(
            attestation.payload,
            attestation.payload_digest,
            attestation.signature,
            self.build_attestation_public_key,
        )
        immutable_reference = attestation.payload.get("immutable_artifact_reference")
        if not isinstance(immutable_reference, str) or not immutable_reference.endswith(
            f"@{readback.app_image_digest}"
        ):
            raise ProducerHandoffRejected(
                "active build attestation does not pin the running ECR digest"
            )
        old_fingerprint = fingerprint(deploy.payload)
        receipt_validity = min(deploy.expires_at, verified.expires_at) - now
        active = {
            "receipt_id": str(deploy.id),
            "receipt_digest": deploy.payload_digest,
            "verification_receipt_id": str(verified.id),
            "verification_receipt_digest": verified.payload_digest,
        }
        if old_fingerprint != pointer.deployment_fingerprint:
            raise ProducerHandoffRejected("runtime drift requires a new operation")
        if receipt_validity.total_seconds() > minimum_validity_seconds:
            return {"action": "keep", "current_active_pair": active}
        return {"action": "refresh", "current_active_pair": active}

    async def create(
        self,
        *,
        actor_user_id: UUID,
        operation_id: UUID,
        receipt_id: UUID,
        verification_receipt_id: UUID,
        assignment_id: UUID,
        authorization_id: UUID,
        readback: ProducerReadback,
        build_attestation_id: UUID,
        build_attestation_digest: str,
        commit_sha: str,
        tree_sha: str,
        workflow_id: str,
        expected_current: Mapping[str, str] | None,
        now: datetime | None = None,
    ) -> BillingProducerReceipt:
        now = now or datetime.now(timezone.utc)
        pointer = await self.repository.get_active_pair(for_update=True)
        supersedes: dict[str, str] | None = None
        if pointer is None:
            if expected_current is not None:
                raise ProducerHandoffRejected(
                    "active pair disappeared before create compare-and-swap"
                )
        else:
            if pointer.operation_id != operation_id or expected_current is None:
                raise ProducerHandoffRejected(
                    "active pair changed before create compare-and-swap"
                )
            supersedes = {
                "receipt_id": str(pointer.receipt_id),
                "receipt_digest": pointer.receipt_digest,
                "verification_receipt_id": str(pointer.verification_receipt_id),
                "verification_receipt_digest": pointer.verification_receipt_digest,
            }
            if dict(expected_current) != supersedes:
                raise ProducerHandoffRejected(
                    "active pair changed before create compare-and-swap"
                )

        scope = receipt_scope(
            operation_id=operation_id,
            receipt_id=receipt_id,
            verification_receipt_id=verification_receipt_id,
            readback=readback,
            build_attestation_id=build_attestation_id,
            build_attestation_digest=build_attestation_digest,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            workflow_id=workflow_id,
            supersedes=supersedes,
        )
        attestation = await self.repository.get_build_attestation(
            attestation_id=build_attestation_id,
            attestation_digest=build_attestation_digest,
        )
        expected_attestation = {
            "attestation_type": "billing_build_attestation.v1",
            "app_image_digest": readback.app_image_digest,
            "bundle_digest": readback.bundle_digest,
            "commit_sha": commit_sha,
            "tree_sha": tree_sha,
            "workflow_id": workflow_id,
        }
        if attestation is None or any(
            attestation.payload.get(field) != value
            for field, value in expected_attestation.items()
        ):
            raise ProducerHandoffRejected(
                "approved immutable build attestation does not match runtime"
            )
        immutable_reference = attestation.payload.get("immutable_artifact_reference")
        if not isinstance(immutable_reference, str) or not immutable_reference.endswith(
            f"@{readback.app_image_digest}"
        ):
            raise ProducerHandoffRejected(
                "build attestation does not pin the running ECR digest"
            )
        verify_receipt_signature(
            attestation.payload,
            attestation.payload_digest,
            attestation.signature,
            self.build_attestation_public_key,
        )
        if pointer is not None:
            old = await self.repository.get_receipt(
                receipt_id=pointer.receipt_id,
                operation_id=operation_id,
                receipt_type=DEPLOYED_TYPE,
            )
            old_verified = await self.repository.get_receipt(
                receipt_id=pointer.verification_receipt_id,
                operation_id=operation_id,
                receipt_type=VERIFIED_TYPE,
            )
            if (
                old is None
                or old_verified is None
                or old.payload_digest != pointer.receipt_digest
                or old_verified.payload_digest != pointer.verification_receipt_digest
                or old_verified.payload.get("source_receipt_id") != str(old.id)
                or old_verified.payload.get("source_receipt_digest")
                != old.payload_digest
                or old.readback_digest != scope["readback_digest"]
                or fingerprint(old.payload) != pointer.deployment_fingerprint
                or fingerprint(old.payload) != fingerprint(scope)
            ):
                raise ProducerHandoffRejected("runtime drift requires a new operation")
            await self.repository.verify_receipt_signature_only(
                old, self.receipt_public_key
            )
            await self.repository.verify_receipt_signature_only(
                old_verified, self.receipt_public_key
            )
            validity = min(old.expires_at, old_verified.expires_at) - now
            if validity > timedelta(seconds=300):
                raise ProducerHandoffRejected("active pair does not require refresh")

        await self.repository.validate_and_consume_grants(
            assignment_id=assignment_id,
            authorization_id=authorization_id,
            actor_user_id=actor_user_id,
            operation_id=operation_id,
            action=CREATE_ACTION,
            receipt_id=receipt_id,
            verification_receipt_id=verification_receipt_id,
            scope_digest=digest_json(scope),
            now=now,
            authorization_public_key=self.authorization_public_key,
        )
        # Refresh candidate will be CAS-checked by the independent verifier;
        # this creator only appends the deployment receipt.
        return await self.repository.save_deploy_receipt(
            receipt_id=receipt_id,
            operation_id=operation_id,
            actor_user_id=actor_user_id,
            verification_receipt_id=verification_receipt_id,
            scope=scope,
            now=now,
            receipt_signing_key=self.receipt_signing_key,
        )

    async def verify(
        self,
        *,
        actor_user_id: UUID,
        operation_id: UUID,
        receipt_id: UUID,
        verification_receipt_id: UUID,
        assignment_id: UUID,
        authorization_id: UUID,
        readback: ProducerReadback,
        expected_current: Mapping[str, str] | None = None,
        now: datetime | None = None,
    ) -> BillingProducerReceipt:
        now = now or datetime.now(timezone.utc)
        deploy = await self.repository.get_receipt(
            receipt_id=receipt_id,
            operation_id=operation_id,
            receipt_type=DEPLOYED_TYPE,
        )
        if deploy is None:
            raise ProducerHandoffRejected("producer receipt is unavailable")
        await self.repository.validate_receipt(deploy, self.receipt_public_key)
        if deploy.attestor_user_id == actor_user_id:
            raise ProducerHandoffRejected(
                "deployment and verification actors must differ"
            )
        readback_payload = readback.as_payload()
        if digest_json(readback_payload) != deploy.readback_digest or fingerprint(
            deploy.payload
        ) != fingerprint({**deploy.payload, "readback": readback_payload}):
            raise ProducerHandoffRejected(
                "independent live readback differs from receipt"
            )

        pointer = await self.repository.get_active_pair(for_update=True)
        if expected_current is None:
            if pointer is not None:
                raise ProducerHandoffRejected(
                    "active pair exists; refresh requires CAS inputs"
                )
            supersedes: Mapping[str, str | None] | None = None
        else:
            if pointer is None:
                raise ProducerHandoffRejected(
                    "active pair disappeared before verification"
                )
            if any(
                (
                    pointer.operation_id != operation_id,
                    str(pointer.receipt_id) != expected_current.get("receipt_id"),
                    pointer.receipt_digest != expected_current.get("receipt_digest"),
                    str(pointer.verification_receipt_id)
                    != expected_current.get("verification_receipt_id"),
                    pointer.verification_receipt_digest
                    != expected_current.get("verification_receipt_digest"),
                )
            ):
                raise ProducerHandoffRejected(
                    "active pair changed before compare-and-swap"
                )
            expected_old = {
                "receipt_id": str(pointer.receipt_id),
                "receipt_digest": pointer.receipt_digest,
                "verification_receipt_id": str(pointer.verification_receipt_id),
                "verification_receipt_digest": pointer.verification_receipt_digest,
            }
            supersedes = expected_old
            if deploy.payload.get("supersedes") != expected_old:
                raise ProducerHandoffRejected("refresh supersedes links are invalid")
            old_deploy = await self.repository.get_receipt(
                receipt_id=pointer.receipt_id,
                operation_id=operation_id,
                receipt_type=DEPLOYED_TYPE,
            )
            old_verified = await self.repository.get_receipt(
                receipt_id=pointer.verification_receipt_id,
                operation_id=operation_id,
                receipt_type=VERIFIED_TYPE,
            )
            if (
                old_deploy is None
                or old_deploy.payload_digest != pointer.receipt_digest
                or old_deploy.readback_digest != deploy.readback_digest
                or old_verified is None
                or old_verified.payload_digest != pointer.verification_receipt_digest
                or old_verified.payload.get("source_receipt_id")
                != str(pointer.receipt_id)
                or old_verified.payload.get("source_receipt_digest")
                != pointer.receipt_digest
            ):
                raise ProducerHandoffRejected(
                    "superseded verification receipt is invalid"
                )
            await self.repository.verify_receipt_signature_only(
                old_deploy, self.receipt_public_key
            )
            await self.repository.verify_receipt_signature_only(
                old_verified, self.receipt_public_key
            )

        verify_scope = {
            "operation_id": str(operation_id),
            "receipt_id": str(receipt_id),
            "receipt_digest": deploy.payload_digest,
            "verification_receipt_id": str(verification_receipt_id),
            "readback_digest": digest_json(readback_payload),
            "deployment_fingerprint": fingerprint(deploy.payload),
            "supersedes": dict(supersedes) if supersedes is not None else None,
        }
        await self.repository.validate_and_consume_grants(
            assignment_id=assignment_id,
            authorization_id=authorization_id,
            actor_user_id=actor_user_id,
            operation_id=operation_id,
            action=VERIFY_ACTION,
            receipt_id=receipt_id,
            verification_receipt_id=verification_receipt_id,
            scope_digest=digest_json(verify_scope),
            now=now,
            authorization_public_key=self.authorization_public_key,
        )
        verification_payload = {
            "operation_id": str(operation_id),
            "receipt_type": VERIFIED_TYPE,
            "receipt_id": str(verification_receipt_id),
            "source_receipt_id": str(deploy.id),
            "source_receipt_digest": deploy.payload_digest,
            "environment": ENVIRONMENT,
            "operational_role": OPERATIONAL_ROLE,
            "cluster_arn": CLUSTER_ARN,
            "service_arn": SERVICE_ARN,
            "deployment_id": deploy.deployment_id,
            "task_set_id": deploy.task_set_id,
            "config_generation": deploy.config_generation,
            "compatibility_generation": deploy.compatibility_generation,
            "app_digest": deploy.app_digest,
            "bundle_digest": deploy.bundle_digest,
            "readback_digest": digest_json(readback_payload),
            "build_attestation_id": str(deploy.build_attestation_id),
            "build_attestation_digest": deploy.build_attestation_digest,
            "commit_sha": deploy.commit_sha,
            "tree_sha": deploy.tree_sha,
            "workflow_id": deploy.workflow_id,
            "attestor_user_id": str(deploy.attestor_user_id),
            "verifier_user_id": str(actor_user_id),
            "supersedes": dict(supersedes) if supersedes is not None else None,
            "issued_at": now.isoformat(),
            "expires_at": receipt_expiry(now).isoformat(),
        }
        payload_digest, signature = sign_receipt(
            verification_payload, self.receipt_signing_key
        )
        verified = BillingProducerReceipt(
            id=verification_receipt_id,
            operation_id=operation_id,
            receipt_type=VERIFIED_TYPE,
            environment=ENVIRONMENT,
            operational_role=OPERATIONAL_ROLE,
            cluster_arn=CLUSTER_ARN,
            service_arn=SERVICE_ARN,
            deployment_id=deploy.deployment_id,
            task_set_id=deploy.task_set_id,
            config_generation=deploy.config_generation,
            compatibility_generation=deploy.compatibility_generation,
            app_digest=deploy.app_digest,
            bundle_digest=deploy.bundle_digest,
            readback_digest=digest_json(readback_payload),
            build_attestation_id=deploy.build_attestation_id,
            build_attestation_digest=deploy.build_attestation_digest,
            commit_sha=deploy.commit_sha,
            tree_sha=deploy.tree_sha,
            workflow_id=deploy.workflow_id,
            attestor_user_id=deploy.attestor_user_id,
            verifier_user_id=actor_user_id,
            supersedes_receipt_id=(
                UUID(supersedes["receipt_id"]) if supersedes is not None else None
            ),
            supersedes_receipt_digest=(
                supersedes["receipt_digest"] if supersedes is not None else None
            ),
            supersedes_verification_receipt_id=(
                UUID(supersedes["verification_receipt_id"])
                if supersedes is not None
                else None
            ),
            supersedes_verification_receipt_digest=(
                supersedes["verification_receipt_digest"]
                if supersedes is not None
                else None
            ),
            issued_at=now,
            expires_at=now + RECEIPT_TTL,
            payload=verification_payload,
            payload_digest=payload_digest,
            signature=signature,
        )
        self.repository.session.add(verified)
        await self.repository.session.flush([verified])
        await self.repository.activate_verified_pair(
            operation_id=operation_id,
            deploy_receipt=deploy,
            verification_receipt=verified,
            expected_current=expected_current,
            deploy_public_key=self.receipt_public_key,
        )
        return verified
