from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.api.routes import billing_producer as route
from src.models.billing_producer_signed import (
    BillingProducerV1ActivePair,
    BillingProducerV1BuildAttestation,
    BillingProducerV1Grant,
    BillingProducerV1GrantConsumption,
    BillingProducerV1GrantRevocation,
    BillingProducerV1Receipt,
)
from src.models.user import User, UserRole
from src.services.billing_producer_grants import (
    ProducerGrantRejected,
    preflight_grant_scope,
)
from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    PREFLIGHT_ACTION,
    SECRET_REFERENCE_NAMES,
    SERVICE_ARN,
    ProducerHandoffRejected,
    ProducerReadback,
)
from src.services.billing_signed_envelope import jcs, sha256_hex


def _request_body(*, config_generation: str) -> route.ProducerPreflightRequest:
    task_id = uuid4().hex
    readback = route.ProducerReadbackRequest(
        cluster_arn=CLUSTER_ARN,
        service_arn=SERVICE_ARN,
        deployment_id="ecs-svc/active-v1",
        deployment_controller="ECS",
        task_definition_arn=(
            "arn:aws:ecs:us-east-1:388460142431:task-definition/"
            "neurostack-staging-app:8"
        ),
        task_arns=(
            "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/" f"{task_id}",
        ),
        config_generation=config_generation,
        compatibility_generation="compat-v1",
        app_image_digest="sha256:" + "a" * 64,
        ecr_image_digest="sha256:" + "a" * 64,
        bundle_digest="b" * 64,
        readiness="ready",
        rollout_state="COMPLETED",
        rollout_control="disabled",
        topup_commercial_enabled=False,
        secret_references=[
            {
                "name": name,
                "arn": (
                    "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
                    f"{secret_path}-AbCdEf"
                ),
                "version_id": "task-definition-unpinned",
            }
            for name, secret_path in SECRET_REFERENCE_NAMES.items()
        ],
    )
    return route.ProducerPreflightRequest(
        operation_id=uuid4(),
        assignment_id=uuid4(),
        authorization_receipt_id=uuid4(),
        readback=readback,
        minimum_grant_validity_seconds=240,
        minimum_pair_validity_seconds=300,
    )


class _Scalars:
    def __init__(self, values: list[object]):
        self.values = values

    def all(self) -> list[object]:
        return self.values


class _Result:
    def __init__(self, values: list[object]):
        self.values = values

    def scalars(self) -> _Scalars:
        return _Scalars(self.values)


class _Session:
    def __init__(self, grants: list[object], users: list[object]):
        self.grants = grants
        self.users = users
        self.added: list[object] = []
        self.executed_entities: list[type[object]] = []

    async def execute(self, statement: object) -> _Result:
        entity = statement.column_descriptions[0]["entity"]
        self.executed_entities.append(entity)
        if entity is BillingProducerV1Grant:
            return _Result(self.grants)
        if entity is User:
            return _Result(self.users)
        raise AssertionError(f"unexpected query before scope validation: {entity}")

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        raise AssertionError("mismatch must be rejected before flush")


class _RouteSession:
    """Small ORM-shaped session for proving preflight's persistence boundary."""

    def __init__(self, values: dict[type[object], list[object]]):
        self.values = values
        self.added: list[object] = []
        self.queried: list[type[object]] = []

    async def execute(self, statement: object) -> _Result:
        entity = statement.column_descriptions[0]["entity"]
        self.queried.append(entity)
        return _Result(self.values.get(entity, []))

    async def scalar(self, statement: object) -> object | None:
        entity = statement.column_descriptions[0]["entity"]
        self.queried.append(entity)
        values = self.values.get(entity, [])
        return values[0] if values else None

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        raise AssertionError("expired attestation must fail before flush")

    def begin(self) -> _Transaction:
        return _Transaction()


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        ("2020-01-01T00:00:00Z", False),
        ("not-a-timestamp", False),
        (None, True),
    ],
)
def test_preflight_requires_unexpired_build_attestation(
    expires_at: str | None, expected: bool
) -> None:
    now = datetime.now(timezone.utc)
    if expires_at is None:
        expires_at = (now + timedelta(minutes=1)).isoformat()

    assert (
        route._attestation_is_unexpired({"expires_at": expires_at}, now=now) is expected
    )


@pytest.mark.asyncio
async def test_preflight_route_rejects_expired_active_attestation_before_keep_or_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_id = uuid4()
    issuer_id, executor_id, approver_id = uuid4(), uuid4(), uuid4()
    request = _request_body(config_generation="active-v1").model_copy(
        update={"operation_id": operation_id}
    )
    scope = preflight_grant_scope(
        operation_id=operation_id,
        readback=request.readback.as_domain(),
        minimum_grant_validity_seconds=request.minimum_grant_validity_seconds,
        minimum_pair_validity_seconds=request.minimum_pair_validity_seconds,
    )
    grant_envelope = {
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "payload": {"scope": scope},
    }
    assignment = SimpleNamespace(
        id=request.assignment_id,
        grant_kind="assignment",
        issuer_user_id=issuer_id,
        executor_user_id=executor_id,
        approver_user_id=None,
        assignment_id=None,
        assignment_digest=None,
        operation_id=operation_id,
        action=PREFLIGHT_ACTION,
        scope=scope,
        scope_digest=sha256_hex(jcs(scope)),
        envelope=grant_envelope,
        envelope_digest="a" * 64,
    )
    authorization = SimpleNamespace(
        id=request.authorization_receipt_id,
        grant_kind="authorization",
        issuer_user_id=issuer_id,
        executor_user_id=executor_id,
        approver_user_id=approver_id,
        assignment_id=assignment.id,
        assignment_digest=assignment.envelope_digest,
        operation_id=operation_id,
        action=PREFLIGHT_ACTION,
        scope=scope,
        scope_digest=assignment.scope_digest,
        envelope=grant_envelope,
        envelope_digest="c" * 64,
    )
    attestation_id = uuid4()
    attestation = SimpleNamespace(
        id=attestation_id,
        operation_id=operation_id,
        envelope={
            "expires_at": "2020-01-01T00:00:00Z",
            "payload": {"attestation_id": str(attestation_id)},
        },
        envelope_digest="d" * 64,
        payload_digest="e" * 64,
    )
    active_pair = SimpleNamespace(
        operation_id=operation_id,
        receipt_id=uuid4(),
        receipt_digest="f" * 64,
        verification_receipt_id=uuid4(),
        verification_receipt_digest="1" * 64,
    )
    deploy = SimpleNamespace(
        id=active_pair.receipt_id,
        operation_id=operation_id,
        envelope_digest=active_pair.receipt_digest,
        build_attestation_id=attestation_id,
        build_attestation_digest=attestation.envelope_digest,
        app_digest=request.readback.app_image_digest,
        bundle_digest=request.readback.bundle_digest,
        executor_user_id=executor_id,
        envelope={"payload": {}},
    )
    verified = SimpleNamespace(
        id=active_pair.verification_receipt_id,
        operation_id=operation_id,
        envelope_digest=active_pair.verification_receipt_digest,
        verifier_user_id=approver_id,
        envelope={"payload": {}},
    )
    session = _RouteSession(
        {
            BillingProducerV1Grant: [assignment, authorization],
            User: [
                SimpleNamespace(is_active=True, role=UserRole.super_admin)
                for _ in range(3)
            ],
            BillingProducerV1GrantConsumption: [],
            BillingProducerV1GrantRevocation: [],
            BillingProducerV1ActivePair: [active_pair],
            BillingProducerV1Receipt: [deploy, verified],
            BillingProducerV1BuildAttestation: [attestation],
        }
    )
    monkeypatch.setattr(route, "_require_short_staging_jwt", lambda *_args: executor_id)
    monkeypatch.setattr(route, "_v1_grant_row_is_valid", lambda _row: True)
    monkeypatch.setattr(
        route, "_v1_receipt_row_is_valid", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(route, "_v1_trust_entry", lambda _type: object())
    monkeypatch.setattr(route, "TrustRegistry", lambda _entries: object())

    def validate_grants(**kwargs: object) -> None:
        assert (
            kwargs["minimum_grant_validity_seconds"]
            == request.minimum_grant_validity_seconds
        )
        assert (
            kwargs["minimum_pair_validity_seconds"]
            == request.minimum_pair_validity_seconds
        )

    monkeypatch.setattr(route, "validate_preflight_grant_pair", validate_grants)
    monkeypatch.setattr(
        route,
        "verify_envelope",
        lambda *_args, **_kwargs: SimpleNamespace(
            payload=attestation.envelope["payload"]
        ),
    )
    monkeypatch.setattr(route, "validate_attestation_trust", lambda *_args: None)
    monkeypatch.setattr(
        route, "envelope_digest", lambda _envelope: attestation.envelope_digest
    )
    monkeypatch.setattr(
        route, "payload_digest", lambda _payload: attestation.payload_digest
    )

    with pytest.raises(HTTPException) as error:
        await route.producer_pair_preflight(
            request, SimpleNamespace(), tenant=SimpleNamespace(), db=session
        )

    assert error.value.status_code == 409
    assert (
        error.value.detail["reason"]
        == "active pair does not match current readback/provenance"
    )
    assert BillingProducerV1GrantConsumption in session.queried
    assert BillingProducerV1BuildAttestation in session.queried
    assert session.added == []


@pytest.mark.asyncio
async def test_snapshot_mismatch_is_rejected_before_any_consumption_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_id = uuid4()
    issuer_id, executor_id, approver_id = uuid4(), uuid4(), uuid4()
    request = _request_body(config_generation="observed-v2")
    assignment_scope = preflight_grant_scope(
        operation_id=operation_id,
        readback=ProducerReadback(
            **{
                **request.readback.as_domain().__dict__,
                "config_generation": "approved-v1",
            }
        ),
        minimum_grant_validity_seconds=request.minimum_grant_validity_seconds,
        minimum_pair_validity_seconds=request.minimum_pair_validity_seconds,
    )
    assignment = SimpleNamespace(
        id=request.assignment_id,
        grant_kind="assignment",
        issuer_user_id=issuer_id,
        executor_user_id=executor_id,
        approver_user_id=None,
        assignment_id=None,
        assignment_digest=None,
        operation_id=operation_id,
        action=PREFLIGHT_ACTION,
        scope=assignment_scope,
        scope_digest=sha256_hex(jcs(assignment_scope)),
        envelope={"payload": {"scope": assignment_scope}},
        envelope_digest="a" * 64,
    )
    authorization = SimpleNamespace(
        id=request.authorization_receipt_id,
        grant_kind="authorization",
        issuer_user_id=issuer_id,
        executor_user_id=executor_id,
        approver_user_id=approver_id,
        assignment_id=assignment.id,
        assignment_digest=assignment.envelope_digest,
        operation_id=operation_id,
        action=PREFLIGHT_ACTION,
        scope=assignment_scope,
        scope_digest=assignment.scope_digest,
        envelope={"payload": {"scope": assignment_scope}},
        envelope_digest="c" * 64,
    )
    request = request.model_copy(update={"operation_id": operation_id})
    session = _Session(
        [assignment, authorization],
        [
            SimpleNamespace(
                is_active=True,
                role=UserRole.super_admin,
            )
            for _ in (issuer_id, executor_id, approver_id)
        ],
    )
    monkeypatch.setattr(route, "_v1_grant_row_is_valid", lambda _row: True)
    monkeypatch.setattr(route, "_v1_trust_entry", lambda _type: object())

    def reject_changed_snapshot(**kwargs: object) -> None:
        assert kwargs["readback"].config_generation == "observed-v2"
        assert (
            kwargs["minimum_grant_validity_seconds"]
            == request.minimum_grant_validity_seconds
        )
        assert (
            kwargs["minimum_pair_validity_seconds"]
            == request.minimum_pair_validity_seconds
        )
        assert kwargs["assignment_envelope"]["payload"]["scope"] == assignment_scope
        raise ProducerGrantRejected("snapshot differs from signed grant")

    # This isolates the persistence boundary: validation sees the current snapshot
    # and rejects before the endpoint queries or appends either consumption event.
    monkeypatch.setattr(route, "validate_preflight_grant_pair", reject_changed_snapshot)
    with pytest.raises(ProducerHandoffRejected, match="exact readback"):
        await route._canonical_preflight(
            db=session, actor_user_id=executor_id, body=request
        )

    assert session.added == []
    assert session.executed_entities == [BillingProducerV1Grant, User]
