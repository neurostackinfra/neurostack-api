"""Validation rules for v1 producer grants before any consumption event."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    ENVIRONMENT,
    OPERATIONAL_ROLE,
    PREFLIGHT_ACTION,
    SERVICE_ARN,
    ProducerHandoffRejected,
    ProducerReadback,
    digest_json,
)
from src.services.billing_signed_envelope import (
    SignedEnvelopeRejected,
    TrustEntry,
    TrustRegistry,
    envelope_digest,
    jcs,
    sha256_hex,
    verify_envelope,
)


class ProducerGrantRejected(ValueError):
    """A signed grant pair is invalid, stale, or bound to different evidence."""


def preflight_grant_scope(
    *,
    operation_id: UUID,
    readback: ProducerReadback,
    minimum_grant_validity_seconds: int,
    minimum_pair_validity_seconds: int,
) -> dict[str, Any]:
    """Build the signed v1 read-only scope with separate freshness windows."""

    if (
        type(minimum_grant_validity_seconds) is not int
        or not 60 <= minimum_grant_validity_seconds <= 300
        or type(minimum_pair_validity_seconds) is not int
        or minimum_pair_validity_seconds != 300
    ):
        raise ProducerHandoffRejected("preflight validity windows are invalid")
    return {
        "operation_id": str(operation_id),
        "action": PREFLIGHT_ACTION,
        "environment": ENVIRONMENT,
        "operational_role": OPERATIONAL_ROLE,
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "readback_digest": digest_json(readback.as_payload()),
        "minimum_grant_validity_seconds": minimum_grant_validity_seconds,
        "minimum_pair_validity_seconds": minimum_pair_validity_seconds,
    }


def grants_have_minimum_validity(
    *,
    expiries: tuple[datetime, ...],
    now: datetime,
    minimum_grant_validity_seconds: int,
) -> bool:
    """Return whether every signed grant retains the requested lifetime."""

    if (
        now.tzinfo is None
        or now.utcoffset() is None
        or not expiries
        or any(
            expiry.tzinfo is None or expiry.utcoffset() is None for expiry in expiries
        )
        or type(minimum_grant_validity_seconds) is not int
        or not 60 <= minimum_grant_validity_seconds <= 300
    ):
        raise ProducerGrantRejected("grant validity window is invalid")
    instant = now.astimezone(timezone.utc)
    minimum = timedelta(seconds=minimum_grant_validity_seconds)
    return all(
        expiry.astimezone(timezone.utc) - instant >= minimum for expiry in expiries
    )


def producer_pair_preflight_action(
    *,
    deploy_expires_at: datetime,
    verification_expires_at: datetime,
    now: datetime,
    minimum_pair_validity_seconds: int,
) -> str:
    """Use the pair freshness window only to choose keep versus refresh."""

    timestamps = (deploy_expires_at, verification_expires_at, now)
    if (
        any(value.tzinfo is None or value.utcoffset() is None for value in timestamps)
        or type(minimum_pair_validity_seconds) is not int
        or minimum_pair_validity_seconds != 300
    ):
        raise ProducerGrantRejected("producer pair validity window is invalid")
    remaining = min(deploy_expires_at, verification_expires_at) - now
    return (
        "keep"
        if remaining > timedelta(seconds=minimum_pair_validity_seconds)
        else "refresh"
    )


@dataclass(frozen=True)
class ValidatedPreflightGrantPair:
    assignment_payload: Mapping[str, Any]
    authorization_payload: Mapping[str, Any]
    assignment_digest: str
    authorization_digest: str
    scope: Mapping[str, Any]


def validate_preflight_grant_pair(
    *,
    assignment_envelope: Mapping[str, Any],
    authorization_envelope: Mapping[str, Any],
    assignment_digest: str,
    authorization_digest: str,
    assignment_id: UUID,
    authorization_id: UUID,
    actor_user_id: UUID,
    operation_id: UUID,
    readback: ProducerReadback,
    minimum_grant_validity_seconds: int,
    minimum_pair_validity_seconds: int,
    assignment_trust: TrustEntry,
    authorization_trust: TrustEntry,
    now: datetime,
) -> ValidatedPreflightGrantPair:
    """Verify both signatures and the exact read snapshot before event writes."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ProducerGrantRejected("grant validation clock must be timezone-aware")
    if (
        type(minimum_grant_validity_seconds) is not int
        or not 60 <= minimum_grant_validity_seconds <= 300
        or type(minimum_pair_validity_seconds) is not int
        or minimum_pair_validity_seconds != 300
    ):
        raise ProducerGrantRejected("preflight validity windows are invalid")
    try:
        verify_envelope(
            assignment_envelope,
            TrustRegistry([assignment_trust]),
            expected_type="billing_producer_assignment.v1",
        )
        verify_envelope(
            authorization_envelope,
            TrustRegistry([authorization_trust]),
            expected_type="billing_producer_authorization.v1",
        )
        actual_assignment_digest = envelope_digest(assignment_envelope)
        actual_authorization_digest = envelope_digest(authorization_envelope)
        assignment_payload = assignment_envelope["payload"]
        authorization_payload = authorization_envelope["payload"]
    except (SignedEnvelopeRejected, KeyError, TypeError):
        raise ProducerGrantRejected(
            "grant signature or canonical schema is invalid"
        ) from None

    if (
        actual_assignment_digest != assignment_digest
        or actual_authorization_digest != authorization_digest
    ):
        raise ProducerGrantRejected("grant envelope digest does not match request")
    if (
        assignment_payload.get("id") != str(assignment_id)
        or assignment_payload.get("grant_kind") != "assignment"
        or authorization_payload.get("id") != str(authorization_id)
        or authorization_payload.get("grant_kind") != "authorization"
    ):
        raise ProducerGrantRejected("grant IDs or kinds do not match request")
    if (
        assignment_payload.get("operation_id") != str(operation_id)
        or authorization_payload.get("operation_id") != str(operation_id)
        or assignment_envelope.get("operation_id") != str(operation_id)
        or authorization_envelope.get("operation_id") != str(operation_id)
    ):
        raise ProducerGrantRejected("grant operation does not match request")
    if (
        assignment_payload.get("action") != "read_billing_producer_pair"
        or authorization_payload.get("action") != "read_billing_producer_pair"
        or assignment_payload.get("receipt_id") is not None
        or assignment_payload.get("verification_receipt_id") is not None
        or authorization_payload.get("receipt_id") is not None
        or authorization_payload.get("verification_receipt_id") is not None
    ):
        raise ProducerGrantRejected("grant does not authorize read-only preflight")

    issuer_user_id = assignment_payload.get("issuer_user_id")
    executor_user_id = assignment_payload.get("executor_user_id")
    approver_user_id = authorization_payload.get("approver_user_id")
    if (
        executor_user_id != str(actor_user_id)
        or authorization_payload.get("issuer_user_id") != issuer_user_id
        or authorization_payload.get("executor_user_id") != executor_user_id
        or approver_user_id in {issuer_user_id, executor_user_id}
    ):
        raise ProducerGrantRejected("grant actors do not match the authorization")

    if (
        authorization_payload.get("assignment_id") != str(assignment_id)
        or authorization_payload.get("assignment_digest") != assignment_digest
        or any(
            authorization_payload.get(field) != assignment_payload.get(field)
            for field in (
                "issuer_user_id",
                "executor_user_id",
                "operation_id",
                "environment",
                "operational_role",
                "cluster_arn",
                "service_arn",
                "action",
                "receipt_id",
                "verification_receipt_id",
                "scope",
                "scope_digest",
            )
        )
    ):
        raise ProducerGrantRejected("authorization does not copy its assignment scope")

    expected_scope = preflight_grant_scope(
        operation_id=operation_id,
        readback=readback,
        minimum_grant_validity_seconds=minimum_grant_validity_seconds,
        minimum_pair_validity_seconds=minimum_pair_validity_seconds,
    )
    expected_scope_digest = sha256_hex(jcs(expected_scope))
    if (
        assignment_payload.get("scope") != expected_scope
        or authorization_payload.get("scope") != expected_scope
        or assignment_payload.get("scope_digest") != expected_scope_digest
        or authorization_payload.get("scope_digest") != expected_scope_digest
    ):
        raise ProducerGrantRejected(
            "readback or validity window differs from grant scope"
        )

    issued_at = _parse_envelope_time(assignment_envelope, "issued_at")
    assignment_expires_at = _parse_envelope_time(assignment_envelope, "expires_at")
    authorization_issued_at = _parse_envelope_time(authorization_envelope, "issued_at")
    authorization_expires_at = _parse_envelope_time(
        authorization_envelope, "expires_at"
    )
    instant = now.astimezone(timezone.utc)
    if (
        instant < issued_at
        or instant < authorization_issued_at
        or authorization_expires_at > assignment_expires_at
    ):
        raise ProducerGrantRejected(
            "assignment or authorization is outside its validity"
        )
    if instant >= assignment_expires_at or instant >= authorization_expires_at:
        raise ProducerGrantRejected("grant validity window expired")

    return ValidatedPreflightGrantPair(
        assignment_payload=assignment_payload,
        authorization_payload=authorization_payload,
        assignment_digest=actual_assignment_digest,
        authorization_digest=actual_authorization_digest,
        scope=expected_scope,
    )


def _parse_envelope_time(envelope: Mapping[str, Any], field: str) -> datetime:
    value = envelope.get(field)
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProducerGrantRejected("grant timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ProducerGrantRejected("grant timestamp is invalid") from None
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ProducerGrantRejected("grant timestamp is not UTC")
    return parsed.astimezone(timezone.utc)
