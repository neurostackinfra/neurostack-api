"""Closed, read-only boundaries used by the staging producer rehearsal.

This module deliberately has no AWS SDK dependency. The live adapter exposes
only the control-plane queries needed by the rehearsal and never accepts an
arbitrary AWS operation. The two command-line scripts are thin facades over
these contracts so the same negative guarantees are exercised in unit tests.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID, uuid4

ENVIRONMENT = "staging"
ACCOUNT_ID = "388460142431"
EXPECTED_CALLER_ARN = "arn:aws:iam::388460142431:user/lorenzo"
REGION = "us-east-1"
CLUSTER_ARN = "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging"
SERVICE_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:service/"
    "neurostack-staging/neurostack-staging-app"
)
STAGING_API_ORIGIN = "https://staging.api.neurostack.global"
STAGING_ROLLOUT_READBACK_PATH = "/v1/admin/billing/rollout/readback"
STAGING_API_RESPONSE_MAX_BYTES = 16 * 1024
STAGING_API_TIMEOUT_SECONDS = 8.0
STAGING_SCOPE = "billing.producer.staging.rehearsal"
STAGING_RECEIPT_TYPE = "billing_producer_staging_rehearsed.v1"
STAGING_VERIFIED_RECEIPT_TYPE = "billing_producer_staging_rehearsal_verified.v1"
PRODUCTION_RECEIPT_TYPES = {
    "billing_producer_deployed.v1",
    "billing_producer_deploy_verified.v1",
}

SECRET_REFERENCE_NAMES = {
    "DATABASE_URL": "neurostack/staging/database-url",
    "REDIS_URL": "neurostack/staging/redis-url",
    "SECRET_KEY": "neurostack/staging/secret-key",
    "LLM_API_KEY": "neurostack/staging/llm-api-key",
    "BILLING_OPERATION_RECEIPT_SIGNING_KEY": (
        "neurostack/staging/billing-operation-receipt-signing-key"
    ),
}
SECRET_NAME_TO_ENV = {value: key for key, value in SECRET_REFERENCE_NAMES.items()}
SECRET_ENV_NAMES = frozenset(SECRET_REFERENCE_NAMES)
SECRET_ARN_RE = re.compile(
    r"^arn:aws:secretsmanager:(?P<region>[a-z0-9-]+):(?P<account>[0-9]{12}):"
    r"secret:(?P<name>[^:]+)$"
)
SECRET_SUFFIX_RE = re.compile(r"^(?P<name>.+)-[A-Za-z0-9]{6}$")
SECRET_VALUE_RE = re.compile(
    r"(?:postgres(?:ql)?(?:\+asyncpg)?://|redis://|arn:aws:secretsmanager:[^\s]+)"
    r"|(?:^|[\n;])(?:DATABASE_URL|REDIS_URL|SECRET_KEY|LLM_API_KEY|"
    r"BILLING_OPERATION_RECEIPT_SIGNING_KEY)="
    r"[^\s;]+",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class RehearsalError(ValueError):
    """A staging rehearsal contract is invalid or cannot be proven."""


class BlockedRehearsal(RehearsalError):
    """The target is intentionally not healthy enough for a positive run."""


@dataclass(frozen=True)
class Target:
    """The only AWS target accepted by this rehearsal."""

    environment: str
    account_id: str
    region: str
    cluster_arn: str
    service_arn: str
    expected_caller_arn: str = EXPECTED_CALLER_ARN

    def validate(self) -> None:
        if self.environment != ENVIRONMENT:
            raise RehearsalError("only staging is accepted")
        expected = {
            "account_id": ACCOUNT_ID,
            "region": REGION,
            "cluster_arn": CLUSTER_ARN,
            "service_arn": SERVICE_ARN,
            "expected_caller_arn": EXPECTED_CALLER_ARN,
        }
        actual = {
            "account_id": self.account_id,
            "region": self.region,
            "cluster_arn": self.cluster_arn,
            "service_arn": self.service_arn,
            "expected_caller_arn": self.expected_caller_arn,
        }
        if actual != expected:
            raise RehearsalError("AWS target is outside the staging allowlist")

    def public(self) -> dict[str, str]:
        self.validate()
        return {
            "environment": self.environment,
            "account_id": self.account_id,
            "region": self.region,
            "cluster_arn": self.cluster_arn,
            "service_arn": self.service_arn,
        }


def target_from_args(args: Any) -> Target:
    target = Target(
        environment=str(args.environment),
        account_id=str(args.account_id),
        region=str(args.region),
        cluster_arn=str(args.cluster_arn),
        service_arn=str(args.service_arn),
        expected_caller_arn=str(
            getattr(args, "expected_caller_arn", EXPECTED_CALLER_ARN)
        ),
    )
    target.validate()
    return target


class ReadOnlyAws(Protocol):
    """Narrow AWS seam; implementations may only perform approved reads."""

    def query(
        self, service: str, operation: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


READ_ONLY_OPERATIONS: dict[str, frozenset[str]] = {
    "sts": frozenset({"get-caller-identity"}),
    "ecs": frozenset(
        {
            "describe-services",
            "describe-task-definition",
            "list-tasks",
            "describe-tasks",
        }
    ),
    "elbv2": frozenset({"describe-target-groups", "describe-load-balancers"}),
    "secretsmanager": frozenset({"describe-secret"}),
}


class AwsCliReadOnly:
    """AWS CLI adapter with a closed operation allowlist and redacted errors."""

    def __init__(self, *, region: str, runner: Any = subprocess.run) -> None:
        self.region = region
        self._runner = runner

    def query(
        self, service: str, operation: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        allowed = READ_ONLY_OPERATIONS.get(service, frozenset())
        if operation not in allowed:
            raise RehearsalError("AWS operation is not an approved readback")
        command = [
            "aws",
            service,
            operation,
            "--region",
            self.region,
            "--output",
            "json",
            "--no-cli-pager",
        ]
        for key, value in arguments.items():
            option = "--" + key.replace("_", "-")
            command.append(option)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                command.extend(str(item) for item in value)
            else:
                command.append(str(value))
        try:
            result = self._runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            raise RehearsalError("AWS readback could not be executed") from None
        if result.returncode != 0:
            raise RehearsalError("AWS readback failed")
        try:
            decoded = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            raise RehearsalError("AWS readback was not valid JSON") from None
        if not isinstance(decoded, Mapping):
            raise RehearsalError("AWS readback must be a JSON object")
        return decoded


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_secret_name(reference: Any) -> str:
    """Return only an allowlisted semantic name; never return a secret ARN."""

    if not isinstance(reference, str) or not reference:
        raise RehearsalError("secret reference is invalid")
    if reference in SECRET_NAME_TO_ENV:
        return reference
    match = SECRET_ARN_RE.fullmatch(reference)
    if match is None:
        raise RehearsalError("secret reference is not a staging reference")
    if match.group("region") != REGION or match.group("account") != ACCOUNT_ID:
        raise RehearsalError("secret reference is outside the staging target")
    name = match.group("name")
    if name in SECRET_NAME_TO_ENV:
        return name
    suffix_match = SECRET_SUFFIX_RE.fullmatch(name)
    if suffix_match is None or suffix_match.group("name") not in SECRET_NAME_TO_ENV:
        raise RehearsalError("secret reference is not allowlisted")
    return suffix_match.group("name")


def _secret_entries(task_definition: Mapping[str, Any]) -> list[tuple[str, str]]:
    containers = task_definition.get("containerDefinitions")
    if not isinstance(containers, list):
        raise RehearsalError("task definition has no container definitions")
    entries: list[tuple[str, str]] = []
    for container in containers:
        if not isinstance(container, Mapping):
            raise RehearsalError("task definition container is invalid")
        secrets = container.get("secrets", [])
        if not isinstance(secrets, list):
            raise RehearsalError("task definition secrets are invalid")
        for item in secrets:
            if not isinstance(item, Mapping):
                raise RehearsalError("task definition secret entry is invalid")
            if set(item) != {"name", "valueFrom"}:
                raise RehearsalError(
                    "secret metadata contains an unexpected value field"
                )
            name = item.get("name")
            reference = item.get("valueFrom")
            if not isinstance(name, str) or not isinstance(reference, str):
                raise RehearsalError("task definition secret entry is incomplete")
            entries.append((name, reference))
    return entries


def verify_secret_references(task_definition: Mapping[str, Any]) -> dict[str, Any]:
    """Validate five exact references without resolving any value."""

    entries = _secret_entries(task_definition)
    expected = set(SECRET_REFERENCE_NAMES)
    observed: dict[str, str] = {}
    for env_name, reference in entries:
        canonical = canonical_secret_name(reference)
        if env_name in observed or canonical in observed.values():
            raise RehearsalError("staging secret reference is duplicated")
        if env_name not in expected or SECRET_REFERENCE_NAMES[env_name] != canonical:
            raise RehearsalError("secret is not mapped to its staging reference")
        observed[env_name] = canonical
    if set(observed) != expected:
        raise RehearsalError(
            "the five required staging secret references are incomplete"
        )
    return {
        "metadata_only": True,
        "secret_references": [
            {
                "environment_name": env_name,
                "reference_name": observed[env_name],
                "reference_sha256": _sha256(
                    next(reference for name, reference in entries if name == env_name)
                ),
            }
            for env_name in sorted(observed)
        ],
        "values_read": False,
    }


def _redact_string(value: str) -> str:
    if SECRET_VALUE_RE.search(value):
        return "[REDACTED]"
    if SECRET_ARN_RE.fullmatch(value):
        return "[REDACTED_SECRET_REFERENCE]"
    return value


def redact_evidence(value: Any, *, key: str = "") -> Any:
    """Recursively remove secret values and secret ARNs from evidence."""

    lowered = key.lower()
    if key.upper() in SECRET_ENV_NAMES:
        return "[REDACTED]"
    if lowered in {
        "secretstring",
        "secretbinary",
        "secretvalue",
        "valuefrom",
        "secret_arn",
    }:
        return "[REDACTED]"
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return {str(k): redact_evidence(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_evidence(item, key=key) for item in value]
    return value


def assert_evidence_safe(value: Any) -> None:
    """Reject raw secret material rather than trusting a caller's redaction."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            secret_field = str(key).upper() in SECRET_ENV_NAMES or str(key).lower() in {
                "secretstring",
                "secretbinary",
                "secretvalue",
            }
            if secret_field and not (
                isinstance(nested, str)
                and nested in {"[REDACTED]", "[REDACTED_SECRET_REFERENCE]"}
            ):
                raise RehearsalError("evidence contains a secret value")
            assert_evidence_safe(nested)
        return
    if isinstance(value, list):
        for nested in value:
            assert_evidence_safe(nested)
        return
    if isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise RehearsalError("evidence contains secret material")


def safe_json(value: Any) -> str:
    redacted = redact_evidence(value)
    assert_evidence_safe(redacted)
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def require_uuid(value: str, field: str) -> str:
    if not isinstance(value, str) or UUID_RE.fullmatch(value) is None:
        raise RehearsalError(f"{field} must be a UUID")
    return str(UUID(value))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_expiry(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RehearsalError("authorization expiry is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RehearsalError("authorization expiry is invalid") from None
    if parsed.tzinfo is None:
        raise RehearsalError("authorization expiry must be timezone-aware")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class AuthorizationRequest:
    environment: str
    scope: str
    operation_id: str
    assignment_id: str
    authorization_id: str

    def validate(self) -> None:
        if self.environment != ENVIRONMENT:
            raise RehearsalError("authorization is not for staging")
        if self.scope != STAGING_SCOPE:
            raise RehearsalError("authorization scope is not allowlisted")
        for field, value in (
            ("operation_id", self.operation_id),
            ("assignment_id", self.assignment_id),
            ("authorization_id", self.authorization_id),
        ):
            require_uuid(value, field)


@dataclass(frozen=True)
class AuthorizationGrant:
    actor_id: str
    environment: str
    scope: str
    operation_id: str
    assignment_id: str
    authorization_id: str
    expires_at: datetime
    single_use: bool

    @classmethod
    def from_backend(
        cls, response: Mapping[str, Any], request: AuthorizationRequest
    ) -> "AuthorizationGrant":
        request.validate()
        actor_id = response.get("actor_id")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise RehearsalError("backend did not derive an actor")
        if response.get("environment") != request.environment:
            raise RehearsalError("backend authorization environment mismatch")
        if response.get("scope") != request.scope:
            raise RehearsalError("backend authorization scope mismatch")
        for field in ("operation_id", "assignment_id", "authorization_id"):
            if response.get(field) != getattr(request, field):
                raise RehearsalError(f"backend authorization {field} mismatch")
        if response.get("single_use") is not True:
            raise RehearsalError("authorization is not single-use")
        return cls(
            actor_id=actor_id,
            environment=request.environment,
            scope=request.scope,
            operation_id=request.operation_id,
            assignment_id=request.assignment_id,
            authorization_id=request.authorization_id,
            expires_at=parse_expiry(response.get("expires_at")),
            single_use=True,
        )


class AuthorizationBackend(Protocol):
    def consume(
        self, jwt_token: str, request: AuthorizationRequest
    ) -> Mapping[str, Any]: ...


class SingleUseAuthorization:
    """Client guard complementing the backend's atomic consume operation."""

    def __init__(self, backend: AuthorizationBackend) -> None:
        self.backend = backend
        self._consumed: set[tuple[str, str]] = set()

    def consume(
        self,
        jwt_token: str,
        request: AuthorizationRequest,
        *,
        now: datetime | None = None,
    ) -> AuthorizationGrant:
        request.validate()
        key = (request.assignment_id, request.authorization_id)
        if key in self._consumed:
            raise RehearsalError("assignment or authorization was already consumed")
        response = self.backend.consume(jwt_token, request)
        grant = AuthorizationGrant.from_backend(response, request)
        current = now or utc_now()
        if grant.expires_at <= current:
            raise RehearsalError("authorization is expired")
        self._consumed.add(key)
        return grant


class ActorSeparation:
    """Require deploy, attestation and verification to be three actors."""

    def __init__(self) -> None:
        self._roles: dict[str, str] = {}

    def bind(self, role: str, grant: AuthorizationGrant) -> None:
        if role not in {"deploy", "attestation", "verification"}:
            raise RehearsalError("unknown rehearsal actor role")
        if role in self._roles:
            raise RehearsalError("rehearsal actor role was already assigned")
        if grant.actor_id in self._roles.values():
            raise RehearsalError("rehearsal actors must be distinct")
        self._roles[role] = grant.actor_id

    def require_complete(self) -> dict[str, str]:
        if set(self._roles) != {"deploy", "attestation", "verification"}:
            raise RehearsalError("three distinct rehearsal actors are required")
        return dict(self._roles)


def target_from_readback(target: Target, identity: Mapping[str, Any]) -> None:
    caller = identity.get("Arn")
    if caller != target.expected_caller_arn:
        raise RehearsalError("AWS caller is outside the staging allowlist")
    account = identity.get("Account")
    if account is not None and str(account) != target.account_id:
        raise RehearsalError("AWS account readback differs from the allowlist")


def _first_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, list) and value and isinstance(value[0], Mapping):
        return value[0]
    return None


def collect_preflight(target: Target, aws: ReadOnlyAws) -> dict[str, Any]:
    """Collect exact, sanitized target readbacks and derive READY/BLOCKED."""

    target.validate()
    identity = aws.query("sts", "get-caller-identity", {})
    target_from_readback(target, identity)
    service_response = aws.query(
        "ecs",
        "describe-services",
        {"cluster": target.cluster_arn, "services": [target.service_arn]},
    )
    services = service_response.get("services")
    service = _first_mapping(services)
    if not isinstance(services, list) or len(services) != 1 or service is None:
        raise RehearsalError("staging service readback is missing or ambiguous")
    if service.get("serviceArn") != target.service_arn:
        raise RehearsalError("service ARN readback differs from the allowlist")
    if service.get("clusterArn") != target.cluster_arn:
        raise RehearsalError("cluster ARN readback differs from the allowlist")
    task_definition_arn = service.get("taskDefinition")
    if not isinstance(task_definition_arn, str) or not task_definition_arn:
        raise RehearsalError("task definition readback is missing")
    task_response = aws.query(
        "ecs", "describe-task-definition", {"task-definition": task_definition_arn}
    )
    task_definition = task_response.get("taskDefinition")
    if not isinstance(task_definition, Mapping):
        raise RehearsalError("task definition readback is invalid")
    secret_metadata = verify_secret_references(task_definition)

    task_arns_response = aws.query(
        "ecs",
        "list-tasks",
        {
            "cluster": target.cluster_arn,
            "service-name": target.service_arn,
            "desired-status": "RUNNING",
        },
    )
    task_arns = task_arns_response.get("taskArns", [])
    if not isinstance(task_arns, list) or any(
        not isinstance(item, str) for item in task_arns
    ):
        raise RehearsalError("running task readback is invalid")
    running_task_digests: list[str] = []
    if task_arns:
        task_response = aws.query(
            "ecs",
            "describe-tasks",
            {"cluster": target.cluster_arn, "tasks": task_arns},
        )
        tasks = task_response.get("tasks", [])
        if not isinstance(tasks, list):
            raise RehearsalError("running task readback is invalid")
        for task in tasks:
            if not isinstance(task, Mapping):
                raise RehearsalError("running task readback is invalid")
            for container in task.get("containers", []):
                if isinstance(container, Mapping) and isinstance(
                    container.get("imageDigest"), str
                ):
                    running_task_digests.append(container["imageDigest"])

    load_balancers: list[dict[str, Any]] = []
    for item in service.get("loadBalancers", []) or []:
        if not isinstance(item, Mapping):
            raise RehearsalError("load balancer readback is invalid")
        target_group_arn = item.get("targetGroupArn")
        if not isinstance(target_group_arn, str):
            continue
        target_groups = aws.query(
            "elbv2",
            "describe-target-groups",
            {"target-group-arns": [target_group_arn]},
        ).get("TargetGroups", [])
        group = _first_mapping(target_groups)
        if group is None:
            raise RehearsalError("target group readback is missing")
        lb_arns = group.get("LoadBalancerArns", [])
        if not isinstance(lb_arns, list) or any(
            not isinstance(v, str) for v in lb_arns
        ):
            raise RehearsalError("load balancer ARN readback is invalid")
        dns_names: list[str] = []
        if lb_arns:
            lbs = aws.query(
                "elbv2",
                "describe-load-balancers",
                {"load-balancer-arns": lb_arns},
            ).get("LoadBalancers", [])
            for load_balancer in lbs:
                if isinstance(load_balancer, Mapping) and isinstance(
                    load_balancer.get("DNSName"), str
                ):
                    dns_names.append(load_balancer["DNSName"])
        load_balancers.append(
            {
                "target_group_arn": target_group_arn,
                "load_balancer_arns": lb_arns,
                "dns_names": dns_names,
            }
        )

    deployments = service.get("deployments", [])
    primary = next(
        (
            item
            for item in deployments
            if isinstance(item, Mapping) and item.get("status") == "PRIMARY"
        ),
        {},
    )
    rollout_state = service.get("rolloutState", primary.get("rolloutState", "UNKNOWN"))
    rollout = service.get("rollout")
    if rollout is None:
        rollout = primary.get("rollout")
    desired = service.get("desiredCount", 0)
    running = service.get("runningCount", 0)
    pending = service.get("pendingCount", 0)
    healthy = (
        rollout_state == "COMPLETED"
        and rollout == "disabled"
        and isinstance(desired, int)
        and desired > 0
        and isinstance(running, int)
        and running == desired
        and isinstance(pending, int)
        and pending == 0
        and bool(task_arns)
    )
    status = "READY" if healthy else "BLOCKED"
    result: dict[str, Any] = {
        "status": status,
        "environment": ENVIRONMENT,
        "target": target.public(),
        "caller_arn_verified": True,
        "task_definition_arn": task_definition_arn,
        "image_digests": sorted(set(running_task_digests)),
        "app_digest": service.get("appDigest", primary.get("appDigest")),
        "bundle_digest": service.get("bundleDigest", primary.get("bundleDigest")),
        "migration_head": service.get("migrationHead", primary.get("migrationHead")),
        "load_balancers": load_balancers,
        "rollout": rollout,
        "rollout_state": rollout_state,
        "desired_count": desired,
        "running_count": running,
        "pending_count": pending,
        "secret_metadata": secret_metadata,
        "readback_only": True,
        "financial_egress": False,
        "production_handoff": False,
    }
    if status == "BLOCKED":
        result["blocked_reason"] = "staging service is not healthy for rehearsal"
    return result


@dataclass(frozen=True)
class ReceiptIds:
    operation_id: str
    receipt_id: str
    verification_receipt_id: str
    readback_receipt_id: str

    @classmethod
    def allocate(cls) -> "ReceiptIds":
        return cls(*(str(uuid4()) for _ in range(4)))

    def validate(self) -> None:
        for field in (
            "operation_id",
            "receipt_id",
            "verification_receipt_id",
            "readback_receipt_id",
        ):
            require_uuid(getattr(self, field), field)


class AppendOnlyReceiptStore:
    """Receipt files are created once and never overwritten."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def append(self, receipt_id: str, payload: Mapping[str, Any]) -> Path:
        require_uuid(receipt_id, "receipt_id")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{receipt_id}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise RehearsalError("receipt id already exists") from None
        try:
            os.write(descriptor, safe_json(payload).encode("utf-8"))
        finally:
            os.close(descriptor)
        return path


def receipt_payload(
    *,
    target: Target,
    preflight: Mapping[str, Any],
    ids: ReceiptIds,
    actors: Mapping[str, str],
) -> dict[str, Any]:
    ids.validate()
    target.validate()
    if preflight.get("status") != "READY":
        raise BlockedRehearsal("a blocked preflight cannot create a receipt")
    if preflight.get("environment") != ENVIRONMENT:
        raise RehearsalError("receipt environment mismatch")
    if preflight.get("rollout") != "disabled":
        raise RehearsalError("receipt requires rollout disabled")
    if (
        set(actors) != {"deploy", "attestation", "verification"}
        or len(set(actors.values())) != 3
    ):
        raise RehearsalError("receipt requires three distinct actors")
    payload: dict[str, Any] = {
        "receipt_type": STAGING_RECEIPT_TYPE,
        "environment": ENVIRONMENT,
        "operation_id": ids.operation_id,
        "receipt_id": ids.receipt_id,
        "verification_receipt_id": ids.verification_receipt_id,
        "readback_receipt_id": ids.readback_receipt_id,
        "target": target.public(),
        "task_definition_arn": preflight.get("task_definition_arn"),
        "image_digests": preflight.get("image_digests", []),
        "rollout": "disabled",
        "financial_egress": False,
        "production_handoff": False,
        "satisfies_production_receipt": False,
        "actors": dict(actors),
        "statement": "staging rehearsal only; does not satisfy production deployment",
    }
    for field in ("app_digest", "bundle_digest", "migration_head"):
        if preflight.get(field) is not None:
            payload[field] = preflight[field]
    return payload


def verify_receipt_payload(
    payload: Mapping[str, Any],
    *,
    target: Target,
    preflight: Mapping[str, Any],
    verification_actor: str,
    expected_ids: ReceiptIds | None = None,
) -> dict[str, Any]:
    target.validate()
    if payload.get("receipt_type") != STAGING_RECEIPT_TYPE:
        raise RehearsalError("receipt type is not staging-only")
    if payload.get("environment") != ENVIRONMENT:
        raise RehearsalError("receipt environment is not staging")
    if payload.get("target") != target.public():
        raise RehearsalError("receipt target mismatch")
    for field in ("task_definition_arn", "image_digests", "rollout"):
        if payload.get(field) != preflight.get(field) and not (
            field == "rollout" and payload.get(field) == "disabled"
        ):
            raise RehearsalError(f"receipt {field} mismatch")
    for field in ("app_digest", "bundle_digest", "migration_head"):
        if field in payload and payload.get(field) != preflight.get(field):
            raise RehearsalError(f"receipt {field} mismatch")
    if expected_ids is not None:
        expected_ids.validate()
        for field in (
            "operation_id",
            "receipt_id",
            "verification_receipt_id",
            "readback_receipt_id",
        ):
            if payload.get(field) != getattr(expected_ids, field):
                raise RehearsalError(f"receipt {field} mismatch")
    if payload.get("financial_egress") is not False:
        raise RehearsalError("receipt financial egress flag is unsafe")
    if payload.get("production_handoff") is not False:
        raise RehearsalError("staging receipt cannot be a production handoff")
    if payload.get("satisfies_production_receipt") is not False:
        raise RehearsalError("staging receipt cannot satisfy production")
    actors = payload.get("actors")
    if (
        not isinstance(actors, Mapping)
        or actors.get("verification") != verification_actor
    ):
        raise RehearsalError("verification actor mismatch")
    if len(set(str(value) for value in actors.values())) != 3:
        raise RehearsalError("receipt actors are not distinct")
    return {
        "receipt_type": STAGING_VERIFIED_RECEIPT_TYPE,
        "environment": ENVIRONMENT,
        "verified_receipt_id": payload.get("receipt_id"),
        "source_receipt_id": payload.get("receipt_id"),
        "production_handoff": False,
        "satisfies_production_receipt": False,
        "verification_actor": verification_actor,
        "statement": "staging rehearsal verification only; production remains locked",
    }


def reject_production_receipt_type(receipt_type: str) -> None:
    if receipt_type in PRODUCTION_RECEIPT_TYPES:
        raise RehearsalError("production receipt types are forbidden in staging")


def validate_backend_url(
    url: str, expected_path: str = STAGING_ROLLOUT_READBACK_PATH
) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "staging.api.neurostack.global"
        or parts.port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.path != expected_path
        or parts.query
        or parts.fragment
        or parts.netloc != "staging.api.neurostack.global"
    ):
        raise RehearsalError("staging backend URL is outside the fixed allowlist")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        fp: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
        new_url: str,
    ) -> Request | None:
        return None


def _staging_https_open(request: Request, timeout: float) -> Any:
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _RejectRedirects(),
    )
    return opener.open(request, timeout=timeout)


def staging_https_json(
    *,
    path: str,
    method: str,
    jwt_token: str,
    payload: Mapping[str, Any] | None = None,
    opener: Any | None = None,
) -> Mapping[str, Any]:
    """Call one fixed staging endpoint with bounded, non-redirecting HTTPS."""
    if path != STAGING_ROLLOUT_READBACK_PATH:
        raise RehearsalError("staging backend path is outside the allowlist")
    if method != "GET" or payload is not None:
        raise RehearsalError("staging backend method is invalid")
    url = f"{STAGING_API_ORIGIN}{path}"
    validate_backend_url(url, expected_path=path)
    data = (
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {jwt_token}",
            "Cache-Control": "no-store",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    selected_opener = opener or _staging_https_open
    try:
        response = selected_opener(request, timeout=STAGING_API_TIMEOUT_SECONDS)
        with response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            headers = response.headers
            content_type = headers.get("Content-Type", "")
            cache_control = headers.get("Cache-Control", "")
            if (
                status != 200
                or content_type.split(";", 1)[0].strip().lower() != "application/json"
                or "no-store" not in cache_control.lower()
            ):
                raise BlockedRehearsal("staging backend readback is unavailable")
            content_length = headers.get("Content-Length")
            if content_length is not None and (
                not content_length.isdecimal()
                or int(content_length) > STAGING_API_RESPONSE_MAX_BYTES
            ):
                raise BlockedRehearsal("staging backend response is invalid")
            raw = response.read(STAGING_API_RESPONSE_MAX_BYTES + 1)
    except BlockedRehearsal:
        raise
    except (http.client.HTTPException, OSError, TimeoutError, ValueError):
        raise BlockedRehearsal("staging backend readback is unavailable") from None
    if len(raw) > STAGING_API_RESPONSE_MAX_BYTES:
        raise BlockedRehearsal("staging backend response is too large")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RehearsalError):
        raise BlockedRehearsal("staging backend response is invalid") from None
    if not isinstance(decoded, Mapping):
        raise BlockedRehearsal("staging backend response is invalid")
    return decoded


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RehearsalError("duplicate JSON key")
        result[key] = value
    return result


def validate_staging_rollout_readback(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Accept only the closed persisted rollout state needed by the rehearsal."""
    expected = {
        "environment",
        "state",
        "rollout_revision",
        "topup_commercial_enabled",
        "topup_revision",
    }
    if set(payload) != expected:
        raise BlockedRehearsal("staging rollout readback schema is invalid")
    rollout_revision = payload.get("rollout_revision")
    topup_revision = payload.get("topup_revision")
    if (
        payload.get("environment") != ENVIRONMENT
        or payload.get("state") != "disabled"
        or payload.get("topup_commercial_enabled") is not False
        or type(rollout_revision) is not int
        or rollout_revision < 0
        or type(topup_revision) is not int
        or topup_revision < 0
    ):
        raise BlockedRehearsal("staging rollout is not confirmed disabled")
    return {
        "environment": ENVIRONMENT,
        "state": "disabled",
        "rollout_revision": rollout_revision,
        "topup_commercial_enabled": False,
        "topup_revision": topup_revision,
    }


def load_jwt_from_fd(fd: int) -> str:
    if fd < 0:
        raise RehearsalError("auth fd is invalid")
    try:
        token = os.read(fd, 8193).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise RehearsalError("short JWT could not be read") from None
    if not token or len(token) > 8192 or "\n" in token or "\r" in token:
        raise RehearsalError("short JWT is invalid")
    return token
