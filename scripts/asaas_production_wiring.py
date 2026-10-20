#!/usr/bin/env python3
"""Read-only plan for the Group 9 D3/D4 ASAAS production wiring.

``check-manifest`` validates the declarative manifest, its reviewed parameters
and the CloudFormation template without AWS access. ``plan`` reads AWS state
(live, or a recorded readback file), compares it with the manifest and prints a
JSON diff: what would change, what is already compliant and what blocks.
``render-taskdef`` prints the proposed task definition request, derived from the
live D3 source revision, with secrets as ARN references only, the otel image
pinned by digest and ``ManagedBy=manual``. ``check`` runs the existing rollout
evidence validators against that request. ``render-migrations-taskdef`` prints
a new ``neurostack-staging-migrations`` revision that bakes exactly
``MIGRATIONS_COMMAND`` on the reviewed bridge image; revision 3 is only read.
The service rollback target is the manifest field ``rollback_target``.

Every AWS request passes through :class:`ReadOnlyAws`, which allows only an
exact list of Describe/Get/List operations and never ``GetSecretValue``;
responses that carry secret material are rejected. Output contains names,
ARNs, digests and the reviewed non-secret parameters only. Nothing here
mutates AWS: applying the plan is a separate, human-approved operation.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts import asaas_producer_rollout_evidence as evidence  # noqa: E402
from scripts import verify_asaas_producer_image_rollout as rollout  # noqa: E402
from src.config.settings import Settings  # noqa: E402

MANIFEST_RELATIVE_PATH = "infra/production/asaas-production-wiring.yaml"
PARAMETERS_RELATIVE_PATH = "infra/production/asaas-production-wiring.params.yaml"
TEMPLATE_RELATIVE_PATH = "infra/production/asaas-production-wiring-secret-access.yaml"
DEFAULT_MANIFEST = _REPOSITORY_ROOT / MANIFEST_RELATIVE_PATH
MANIFEST_SCHEMA = "asaas_production_wiring_manifest.v1"
PARAMETERS_SCHEMA = "asaas_production_wiring_parameters.v1"
READBACK_SCHEMA = "asaas_production_wiring_readback.v1"
PLAN_SCHEMA = "asaas_production_wiring_plan.v1"
CHECK_SCHEMA = "asaas_production_wiring_check.v1"

ACCOUNT_ID = "388460142431"
REGION = "us-east-1"
CLUSTER = "neurostack-staging"
SERVICE = rollout.SERVICE_NAME
TASK_FAMILY = rollout.TASK_FAMILY
APP_CONTAINER = rollout.APP_CONTAINER


def task_definition_arn(family: str, revision: int) -> str:
    return f"arn:aws:ecs:{REGION}:{ACCOUNT_ID}:task-definition/{family}:{revision}"


# The revision the D3 wiring is derived from (task_definition.source_revision).
# It is history, not the rollback target: revision 9 stays derived from it after
# the rollback target moves.
D3_SOURCE_REVISION = 8
D3_SOURCE_TASK_DEFINITION_ARN = task_definition_arn(TASK_FAMILY, D3_SOURCE_REVISION)
# The bridge-image D3 revision the rollback target is re-pinned to.
BRIDGE_REVISION = 9
# The service rollback target lives in the manifest (rollback_target); these
# are the only pins it may hold and the documented step at which each one takes
# effect. Revision 8 runs code that reads columns migration 036 drops, so the
# re-pin to 9 must happen before 036, and only once revision 9 has served and
# its observation period closed (until then 8 is the safe way back).
ROLLBACK_EFFECTIVE_INITIAL = "initial_d3_planning_2026_10_18"
ROLLBACK_EFFECTIVE_BRIDGE = "bridge_revision_9_serving_verified_and_observation_closed"
ROLLBACK_REPIN_BEFORE = "migration_036"
# revision -> (effective_after, (next revision, its effective_after, before) | None)
ROLLBACK_TARGET_PINS: dict[int, tuple[str, tuple[int, str, str] | None]] = {
    D3_SOURCE_REVISION: (
        ROLLBACK_EFFECTIVE_INITIAL,
        (BRIDGE_REVISION, ROLLBACK_EFFECTIVE_BRIDGE, ROLLBACK_REPIN_BEFORE),
    ),
    BRIDGE_REVISION: (ROLLBACK_EFFECTIVE_BRIDGE, None),
}
# New revisions (app and migrations) carry ManagedBy=manual: owner approval A4
# (2026-10-19) moved the two ECS roles to manual management; no code manages the
# task definitions or the service.
TAG_OVERRIDES: Mapping[str, str] = {"ManagedBy": "manual"}
# The otel collector runs by digest, never by tag. The digest itself is the
# manifest field task_definition.otel_image (the deliberate pin of the current
# :latest resolution); only its repository and form are pinned here.
OTEL_CONTAINER = "otel"
_OTEL_IMAGE = re.compile(
    r"^public\.ecr\.aws/aws-observability/aws-otel-collector@sha256:[0-9a-f]{64}$"
)
EXECUTION_ROLE_NAME = "neurostack-staging-ecs-execution"
TASK_ROLE_NAME = "neurostack-staging-ecs-task"
LOAD_BALANCER_NAME = "neurostack-staging-alb"
HOSTED_ZONE_NAME = "api.neurostack.global"
SERVED_HOSTS = (
    "staging.api.neurostack.global",
    "payments.staging.api.neurostack.global",
)
KMS_KEY_ARN = (
    "arn:aws:kms:us-east-1:388460142431:key/90eff49a-c2bf-4836-834f-d5666294d956"
)
STACK_NAME = "neurostack-asaas-production-wiring"
STACK_CAPABILITIES = ("CAPABILITY_IAM",)
# (logical id, type) of every resource the reviewed template creates.
STACK_RESOURCES = frozenset(
    {
        ("BillingObservabilitySmokeTokenSecret", "AWS::SecretsManager::Secret"),
        ("ExecutionRoleWiringSecretRead", "AWS::IAM::Policy"),
    }
)
APPLIED_EXECUTION_OPTIONS = "RetainExceptOnCreate"
_APPLIED_SMOKE_ARN = re.compile(
    r"^arn:aws:secretsmanager:us-east-1:388460142431:secret:"
    r"neurostack/staging/billing-observability-smoke-token-[A-Za-z0-9]{6}$"
)
# The protected deploy path's image repository (equal to
# scripts/asaas_build_release_preflight.BOOTSTRAP_ECR_URI; a test pins it).
# Pinned here so this read-only tool never imports the HTTP-capable preflight.
PRODUCER_IMAGE_REPOSITORY = (
    "388460142431.dkr.ecr.us-east-1.amazonaws.com/"
    "neurostack-asaas-producer-release-producerimagerepository-jqtdjqst9o1j"
)
WIRING_POLICY_NAME = "asaas-production-wiring-secret-read"
TERRAFORM_POLICY_NAME = "segredos-e-logs"
EXECUTION_MANAGED_POLICY_ARN = (
    "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
)
GET_SECRET_VALUE = "secretsmanager:GetSecretValue"

# (environment name, secret name, source, stack parameter)
SECRET_REFERENCES: tuple[tuple[str, str, str, str | None], ...] = (
    (
        "ASAAS_API_KEY",
        "neurostack/staging/asaas-api-key",
        "existing",
        "AsaasApiKeySecretArn",
    ),
    (
        "ASAAS_WEBHOOK_TOKEN",
        "neurostack/staging/asaas-webhook-token",
        "existing",
        "AsaasWebhookTokenSecretArn",
    ),
    (
        "BILLING_OBSERVABILITY_SMOKE_TOKEN",
        "neurostack/staging/billing-observability-smoke-token",
        "created_by_stack",
        None,
    ),
)
SMOKE_TOKEN_ENV = "BILLING_OBSERVABILITY_SMOKE_TOKEN"
RETURN_URL_PARAMETER = "ASAAS_CHECKOUT_RETURN_URL"
WEBHOOK_URL_PARAMETER = "ASAAS_WEBHOOK_URL"
EMAIL_PARAMETER = "ASAAS_WEBHOOK_NOTIFICATION_EMAIL"
IMAGE_PARAMETER = "app_image_override"
REQUIRED_PARAMETERS = (RETURN_URL_PARAMETER, WEBHOOK_URL_PARAMETER, EMAIL_PARAMETER)
RETURN_PATH = "/billing"
WEBHOOK_PATH = "/v1/billing/webhook/asaas"
# environment name -> ("parameter", parameter name) | ("value", literal)
ADDED_ENVIRONMENT: dict[str, tuple[str, str]] = {
    "ASAAS_WEBHOOK_URL": ("parameter", WEBHOOK_URL_PARAMETER),
    "ASAAS_CHECKOUT_RETURN_URL": ("parameter", RETURN_URL_PARAMETER),
    "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": ("parameter", EMAIL_PARAMETER),
    "BILLING_FACT_WORKER_ENABLED": ("value", "false"),
}
ADDED_SECRETS = tuple(row[0] for row in SECRET_REFERENCES)
FORBIDDEN_NAME_PREFIXES = ("ABACATEPAY_", "APPCONFIG_")
FORBIDDEN_NAMES = frozenset({"SPA_ENABLED", "SPA_DIST_DIR"})
STEPS: tuple[tuple[str, str], ...] = (
    ("secret_access_stack", "d3_apply"),
    ("task_definition_revision", "d3_apply"),
    ("financial_egress_closed", "d3_apply"),
    ("signed_image_build_and_attestation", "separate_approval"),
    ("migrations_033_035", "separate_approval"),
    ("service_deployment", "separate_approval"),
    ("spa_serving", "separate_approval"),
    ("webhook_exposure_and_registration", "separate_approval"),
    ("rollout_transitions", "separate_approval"),
)
# The 033-035 window applies exactly revision 035. Migrations 036 and 037 each
# need their own explicit owner instruction (036 before 037, after a fresh
# SELECT showing zero legacy rows), so the window never upgrades to the head.
MIGRATIONS_STEP = "migrations_033_035"
MIGRATIONS_SOURCE_REVISION = "032"
MIGRATIONS_TARGET_REVISION = "035"
MIGRATIONS_COMMAND = ("alembic", "upgrade", MIGRATIONS_TARGET_REVISION)
MIGRATIONS_VERIFICATION = f"read back alembic_version == {MIGRATIONS_TARGET_REVISION}"
MIGRATIONS_EXCLUDED = (
    "036 and 037: each needs its own explicit owner instruction, 036 before "
    "037, after a fresh SELECT showing zero legacy rows"
)
_UNPINNED_UPGRADE = re.compile(r"upgrade\s+(?:heads?\b|[+-])", re.IGNORECASE)
_ALEMBIC_UPGRADE = re.compile(r"alembic\s+upgrade\s+([^\s`'\"),.;]+)", re.IGNORECASE)
_ALEMBIC_VERSION = re.compile(r"alembic_version\s*==\s*(\w+)")
_PINNED_ALEMBIC_REVISION = re.compile(r"^[0-9]{3}$")
# The one-off migrations family. Revision 3 bakes the unpinned upgrade, so it
# is only read (roles, the database-url-migrations secret, sizing, logs) and
# never run: render-migrations-taskdef registers a new revision whose command
# is exactly MIGRATIONS_COMMAND, on the reviewed bridge image.
MIGRATIONS_FAMILY = "neurostack-staging-migrations"
MIGRATIONS_TASK_SOURCE_REVISION = 3
MIGRATIONS_TASK_SOURCE_ARN = task_definition_arn(
    MIGRATIONS_FAMILY, MIGRATIONS_TASK_SOURCE_REVISION
)
MIGRATIONS_DATABASE_SECRET_NAME = "neurostack/staging/database-url-migrations"
_MIGRATIONS_DATABASE_SECRET_ARN = re.compile(
    rf"^arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:"
    rf"{re.escape(MIGRATIONS_DATABASE_SECRET_NAME)}-[A-Za-z0-9]{{6}}$"
)
# The operator entry point of the webhook registration (T1, PR #120). The F3
# block is a fact checked at plan time: reported only while it is absent.
WEBHOOK_REGISTRATION_ENTRY_POINT = "src/ops/asaas_webhook.py"
KNOWN_BLOCKS = {
    "F1_residual_5_database_url_fingerprint": "signed_image_build_and_attestation",
    "F3_webhook_registration_entry_point": "webhook_exposure_and_registration",
}
# Synthetic inputs for the residual 5 probe (never printed): the image is
# normalized out of the runtime fingerprint, the version ID only shapes the
# pinned DATABASE_URL reference the protected deploy writes.
_PROBE_BRIDGE_IMAGE = f"{rollout.SOURCE_IMAGE_REPOSITORY}@sha256:{'0' * 64}"
_PROBE_DIGEST = "sha256:" + "0" * 64
_PROBE_SECRET_VERSION = "00000000-0000-4000-8000-000000000000"

READ_ONLY_OPERATION_PREFIXES = ("Describe", "Get", "List")
FORBIDDEN_OPERATIONS = frozenset(
    {"GetSecretValue", "BatchGetSecretValue", "GetRandomPassword"}
)
ALLOWED_OPERATIONS = frozenset(
    {
        ("sts", "GetCallerIdentity"),
        ("ecs", "DescribeServices"),
        ("ecs", "DescribeTaskDefinition"),
        ("iam", "GetRole"),
        ("iam", "GetRolePolicy"),
        ("iam", "ListRolePolicies"),
        ("iam", "ListAttachedRolePolicies"),
        ("secretsmanager", "DescribeSecret"),
        ("cloudformation", "DescribeStacks"),
        ("cloudformation", "ListStackResources"),
        ("cloudformation", "GetTemplate"),
        ("ecr", "DescribeImages"),
        ("elbv2", "DescribeLoadBalancers"),
        ("elbv2", "DescribeListeners"),
        ("elbv2", "DescribeRules"),
        ("acm", "DescribeCertificate"),
        ("wafv2", "GetWebACLForResource"),
        ("route53", "ListHostedZonesByName"),
        ("route53", "ListResourceRecordSets"),
        ("appconfig", "ListApplications"),
    }
)
_SECRET_RESPONSE_FIELDS = frozenset(
    {"SecretString", "SecretBinary", "SecretValues", "RandomPassword"}
)
_ERROR_KEY = "__error_code__"
_SECRET_ARN_SUFFIX = re.compile(r"-[A-Za-z0-9]{6}$")
_ECR_IMAGE = re.compile(
    r"^(?P<registry>388460142431\.dkr\.ecr\.us-east-1\.amazonaws\.com)/"
    r"(?P<repository>[a-z0-9][a-z0-9._/-]*)"
    r"(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?$"
)
_ECR_DIGEST_REFERENCE = re.compile(
    r"^388460142431\.dkr\.ecr\.us-east-1\.amazonaws\.com/"
    r"[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$"
)
_OPERATOR_EMAIL = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)
# Last-line output guard: none of these may ever be printed.
_SECRET_LIKE_OUTPUT = re.compile(
    r"[$]aact_|-----BEGIN|SecretString|SecretBinary|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|aws_secret_access_key",
    re.IGNORECASE,
)
# The Group 5 template validator also checks the deploy job's trust pins,
# which only exist in the protected deployment environment. ``check`` runs it
# with these well-formed synthetic pins in a scoped overlay and reports only
# its shape verdict; no digest derived from them is ever reported.
_SYNTHETIC_TRUST_PINS = {
    **{
        f"BILLING_PRODUCER_{kind}_KMS_KEY_ARN": (
            f"arn:aws:kms:us-east-1:388460142431:key/"
            f"00000000-0000-4000-8000-00000000000{index}"
        )
        for index, kind in enumerate(("GRANT", "BUILD", "RECEIPT"), start=1)
    },
    **{
        f"BILLING_PRODUCER_{kind}_PUBLIC_KEY_PEM": (
            "-----BEGIN PUBLIC KEY-----\n"
            "c3ludGhldGljLXNoYXBlLWNoZWNrLW9ubHk=\n"
            "-----END PUBLIC KEY-----\n"
        )
        for kind in ("GRANT", "BUILD", "RECEIPT")
    },
}

Transport = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class WiringError(ValueError):
    """The wiring manifest, parameters, or AWS readback is unsafe or ambiguous."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise WiringError(reason)


def _assert_read_only_allowlist() -> None:
    for _service, operation in ALLOWED_OPERATIONS:
        _require(
            operation.startswith(READ_ONLY_OPERATION_PREFIXES)
            and operation not in FORBIDDEN_OPERATIONS,
            "aws_allowlist_not_read_only",
        )


_assert_read_only_allowlist()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_secret_material(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            _require(key not in _SECRET_RESPONSE_FIELDS, "secret_material_in_readback")
            _reject_secret_material(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_material(nested)


def _error_code(response: Any) -> str | None:
    if isinstance(response, dict) and isinstance(response.get(_ERROR_KEY), str):
        return str(response[_ERROR_KEY])
    return None


def _read_key(service: str, operation: str, params: dict[str, Any]) -> str:
    return _canonical([service, operation, params])


# ---------------------------------------------------------------------------
# Read-only AWS access
# ---------------------------------------------------------------------------


class ReadOnlyAws:
    """The only path to AWS: exact allow-list, checked before every request."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self.calls: list[tuple[str, str]] = []

    def call(
        self, service: str, operation: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        _require(
            (service, operation) in ALLOWED_OPERATIONS
            and operation.startswith(READ_ONLY_OPERATION_PREFIXES)
            and operation not in FORBIDDEN_OPERATIONS,
            f"aws_operation_not_allowed:{service}.{operation}",
        )
        self.calls.append((service, operation))
        response = self._transport(service, operation, copy.deepcopy(params))
        _require(isinstance(response, dict), "aws_response_invalid")
        _reject_secret_material(response)
        return response


def boto3_transport() -> Transport:
    """Live transport. Imported lazily so tests and check-manifest need no SDK."""

    try:
        import boto3
        from botocore import xform_name
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        raise WiringError("aws_sdk_unavailable") from None
    session = boto3.session.Session(region_name=REGION)
    config = Config(
        connect_timeout=5,
        read_timeout=15,
        retries={"mode": "standard", "total_max_attempts": 3},
    )
    clients: dict[str, Any] = {}

    def call(service: str, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        _require(
            (service, operation) in ALLOWED_OPERATIONS,
            f"aws_operation_not_allowed:{service}.{operation}",
        )
        if service not in clients:
            clients[service] = session.client(
                service, region_name=REGION, config=config
            )
        method = getattr(clients[service], xform_name(operation))
        try:
            response = method(**params)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            return {_ERROR_KEY: str(code) if code else "unknown"}
        except BotoCoreError:
            raise WiringError(f"aws_call_failed:{service}.{operation}") from None
        response.pop("ResponseMetadata", None)
        decoded = json.loads(json.dumps(response, default=str))
        _require(isinstance(decoded, dict), "aws_response_invalid")
        return dict(decoded)

    return call


def snapshot_transport(path: Path) -> Transport:
    """Replay a recorded read-only readback (one response per exact request)."""

    try:
        raw = evidence._strict_loads(path.read_bytes(), max_bytes=8_000_000)
    except (OSError, evidence.EvidenceError):
        raise WiringError("readback_file_unreadable") from None
    _require(
        isinstance(raw, dict)
        and set(raw)
        == {"schema_version", "account_id", "region", "captured_at_utc", "reads"}
        and raw["schema_version"] == READBACK_SCHEMA
        and raw["account_id"] == ACCOUNT_ID
        and raw["region"] == REGION
        and isinstance(raw["captured_at_utc"], str)
        and isinstance(raw["reads"], list),
        "readback_file_shape_invalid",
    )
    index: dict[str, dict[str, Any]] = {}
    for row in raw["reads"]:
        _require(
            isinstance(row, dict)
            and set(row) == {"service", "operation", "params", "response"}
            and isinstance(row["params"], dict)
            and isinstance(row["response"], dict),
            "readback_file_read_invalid",
        )
        _require(
            (row["service"], row["operation"]) in ALLOWED_OPERATIONS,
            "readback_file_operation_not_allowed",
        )
        _reject_secret_material(row["response"])
        key = _read_key(row["service"], row["operation"], row["params"])
        _require(key not in index, "readback_file_duplicate_read")
        index[key] = row["response"]

    def call(service: str, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        key = _read_key(service, operation, params)
        _require(key in index, f"readback_missing:{service}.{operation}")
        return copy.deepcopy(index[key])

    return call


# ---------------------------------------------------------------------------
# Manifest, parameters and CloudFormation template
# ---------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys and knows CloudFormation tags."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> Any:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in result:
            raise WiringError("yaml_duplicate_key")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


def _cfn_tag(name: str) -> Callable[[_StrictLoader, yaml.Node], dict[str, Any]]:
    def construct(loader: _StrictLoader, node: yaml.Node) -> dict[str, Any]:
        if isinstance(node, yaml.SequenceNode):
            return {name: loader.construct_sequence(node, deep=True)}
        if isinstance(node, yaml.ScalarNode):
            return {name: loader.construct_scalar(node)}
        raise WiringError("yaml_cloudformation_tag_invalid")

    return construct


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
for _tag, _name in (
    ("!Ref", "Ref"),
    ("!Equals", "Fn::Equals"),
    ("!GetAtt", "Fn::GetAtt"),
    ("!Sub", "Fn::Sub"),
):
    _StrictLoader.add_constructor(_tag, _cfn_tag(_name))


def load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise WiringError(f"file_unreadable:{path.name}") from None
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError:
        raise WiringError(f"yaml_invalid:{path.name}") from None


def _exact_keys(value: Any, expected: set[str], location: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == expected, f"{location}_keys")
    assert isinstance(value, dict)
    return value


def _is_text_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item.strip() for item in value)
    )


def _expected_target() -> dict[str, Any]:
    return {
        "account_id": ACCOUNT_ID,
        "region": REGION,
        "cluster": CLUSTER,
        "service": SERVICE,
        "task_family": TASK_FAMILY,
        "execution_role_name": EXECUTION_ROLE_NAME,
        "task_role_name": TASK_ROLE_NAME,
        "application_environment": "staging",
        "operational_role": "production-serving",
        "load_balancer_name": LOAD_BALANCER_NAME,
        "hosted_zone_name": HOSTED_ZONE_NAME,
        "served_hosts": list(SERVED_HOSTS),
    }


def _expected_secrets() -> dict[str, Any]:
    return {
        "kms_key_arn": KMS_KEY_ARN,
        "stack_name": STACK_NAME,
        "template": TEMPLATE_RELATIVE_PATH,
        "execution_role_policy_name": WIRING_POLICY_NAME,
        "terraform_execution_policy_name": TERRAFORM_POLICY_NAME,
        "references": [
            {
                "environment_name": env,
                "secret_name": name,
                "source": source,
                "stack_parameter": parameter,
            }
            for env, name, source, parameter in SECRET_REFERENCES
        ],
    }


def _expected_parameters_section() -> dict[str, Any]:
    return {
        "file": PARAMETERS_RELATIVE_PATH,
        "required": {
            RETURN_URL_PARAMETER: {
                "validator": "Settings.validate_asaas_checkout_return_url",
                "path": RETURN_PATH,
            },
            WEBHOOK_URL_PARAMETER: {
                "validator": "Settings.validate_asaas_webhook_url",
                "path": WEBHOOK_PATH,
            },
            EMAIL_PARAMETER: {"validator": "operator_email", "path": None},
        },
        "optional": {
            IMAGE_PARAMETER: {"validator": "ecr_image_digest_reference", "path": None}
        },
    }


def _expected_task_definition_section() -> dict[str, Any]:
    """Every pinned key; ``otel_image`` is validated by form (its value is data)."""

    return {
        "source_revision": D3_SOURCE_REVISION,
        "app_secrets_added": list(ADDED_SECRETS),
        "app_environment_added": {
            name: {kind: reference}
            for name, (kind, reference) in ADDED_ENVIRONMENT.items()
        },
        "forbidden_name_prefixes": list(FORBIDDEN_NAME_PREFIXES),
        "forbidden_names": sorted(FORBIDDEN_NAMES),
        "image": "preserve_live_unless_override",
        "tag_overrides": dict(TAG_OVERRIDES),
    }


def _expected_migrations_section() -> dict[str, Any]:
    return {
        "family": MIGRATIONS_FAMILY,
        "source_revision": MIGRATIONS_TASK_SOURCE_REVISION,
        "database_secret_name": MIGRATIONS_DATABASE_SECRET_NAME,
        "image": IMAGE_PARAMETER,
        "command": "MIGRATIONS_COMMAND",
        "tag_overrides": dict(TAG_OVERRIDES),
    }


def _validate_rollback_target(section: Any) -> None:
    """Only the pinned revisions, each with its documented effective step."""

    revision = section.get("revision") if isinstance(section, dict) else None
    pin = (
        ROLLBACK_TARGET_PINS.get(revision)
        if type(revision) is int and not isinstance(revision, bool)
        else None
    )
    _require(pin is not None, "manifest_rollback_target_invalid")
    assert isinstance(section, dict) and pin is not None
    effective_after, repin = pin
    expected_keys = {"revision", "effective_after"} | ({"repin"} if repin else set())
    _require(
        set(section) == expected_keys and section["effective_after"] == effective_after,
        "manifest_rollback_target_invalid",
    )
    if repin is not None:
        note = section["repin"]
        _require(
            isinstance(note, dict)
            and set(note) == {"revision", "effective_after", "before", "why"}
            and (note["revision"], note["effective_after"], note["before"]) == repin
            and isinstance(note["why"], str)
            and "036" in note["why"],
            "manifest_rollback_target_invalid",
        )


@dataclass(frozen=True)
class ManifestPins:
    """The reviewed manifest values the planner's code paths read."""

    rollback_revision: int
    rollback_effective_after: str
    rollback_repin_before: str | None
    otel_image: str
    tag_overrides: tuple[tuple[str, str], ...]

    @property
    def rollback_task_definition_arn(self) -> str:
        return task_definition_arn(TASK_FAMILY, self.rollback_revision)


def manifest_pins(manifest: dict[str, Any]) -> ManifestPins:
    """Pins of a manifest that ``validate_manifest`` already accepted."""

    target = manifest["rollback_target"]
    repin = target.get("repin")
    return ManifestPins(
        rollback_revision=int(target["revision"]),
        rollback_effective_after=str(target["effective_after"]),
        rollback_repin_before=str(repin["before"]) if isinstance(repin, dict) else None,
        otel_image=str(manifest["task_definition"]["otel_image"]),
        tag_overrides=tuple(
            sorted(manifest["task_definition"]["tag_overrides"].items())
        ),
    )


def _validate_applied_record(record: Any) -> None:
    applied = _exact_keys(
        record,
        {
            "date",
            "stack_name",
            "change_set_name",
            "execution_options",
            "smoke_token_secret_arn",
            "terraform_policy_unchanged",
        },
        "manifest_applied_record",
    )
    _require(
        isinstance(applied["date"], str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", applied["date"]) is not None
        and applied["stack_name"] == STACK_NAME
        and isinstance(applied["change_set_name"], str)
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,127}", applied["change_set_name"])
        is not None
        and applied["execution_options"] == APPLIED_EXECUTION_OPTIONS
        and isinstance(applied["smoke_token_secret_arn"], str)
        and _APPLIED_SMOKE_ARN.fullmatch(applied["smoke_token_secret_arn"]) is not None
        and applied["terraform_policy_unchanged"] is True,
        "manifest_applied_record_invalid",
    )


def applied_record(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The validated manifest's record of the applied secret access stack."""

    for step in manifest["steps"]:
        if step["id"] == "secret_access_stack":
            record = step.get("applied")
            return dict(record) if isinstance(record, dict) else None
    return None


def validate_manifest(manifest: Any) -> dict[str, Any]:
    """Pin the reviewed manifest; any widening or reordering fails closed."""

    document = _exact_keys(
        manifest,
        {
            "schema_version",
            "context",
            "target",
            "rollback_target",
            "secrets",
            "parameters",
            "task_definition",
            "migrations_task_definition",
            "financial_egress",
            "steps",
            "known_blocks",
        },
        "manifest",
    )
    _require(document["schema_version"] == MANIFEST_SCHEMA, "manifest_schema_invalid")
    _validate_rollback_target(document["rollback_target"])
    context = _exact_keys(document["context"], {"arch", "must", "ref"}, "context")
    _require(
        isinstance(context["arch"], str)
        and _is_text_list(context["must"])
        and isinstance(context["ref"], list)
        and bool(context["ref"]),
        "context_invalid",
    )
    _require(document["target"] == _expected_target(), "manifest_target_mismatch")
    _require(document["secrets"] == _expected_secrets(), "manifest_secrets_mismatch")
    _require(
        document["parameters"] == _expected_parameters_section(),
        "manifest_parameters_mismatch",
    )
    task_definition = document["task_definition"]
    _require(
        isinstance(task_definition, dict)
        and {k: v for k, v in task_definition.items() if k != "otel_image"}
        == _expected_task_definition_section(),
        "manifest_task_definition_mismatch",
    )
    otel_image = task_definition.get("otel_image")
    _require(
        isinstance(otel_image, str) and _OTEL_IMAGE.fullmatch(otel_image) is not None,
        "manifest_otel_image_not_digest",
    )
    _require(
        document["migrations_task_definition"] == _expected_migrations_section(),
        "manifest_migrations_task_definition_mismatch",
    )
    egress = _exact_keys(
        document["financial_egress"],
        {"flag", "initial_state", "mechanism", "why"},
        "financial_egress",
    )
    _require(
        egress["flag"] == "financial_egress_enabled"
        and egress["initial_state"] == "closed"
        and egress["mechanism"] == "closed_by_absence"
        and isinstance(egress["why"], str),
        "manifest_financial_egress_not_closed",
    )
    steps = document["steps"]
    _require(isinstance(steps, list), "manifest_steps_invalid")
    _require(
        [(row.get("id"), row.get("scope")) for row in steps if isinstance(row, dict)]
        == list(STEPS)
        and len(steps) == len(STEPS),
        "manifest_steps_order_or_scope_mismatch",
    )
    for row in steps:
        step_keys = {
            "id",
            "scope",
            "approval",
            "why",
            "preconditions",
            "change",
            "verification",
            "rollback",
        }
        # Only the secret access stack has been applied (2026-10-18); its
        # record is mandatory and no other step may claim one.
        if isinstance(row, dict) and row.get("id") == "secret_access_stack":
            step_keys.add("applied")
        step = _exact_keys(row, step_keys, "manifest_step")
        if "applied" in step:
            _validate_applied_record(step["applied"])
        _require(
            all(
                isinstance(step[key], str) and step[key].strip()
                for key in ("approval", "why", "change", "rollback")
            )
            and _is_text_list(step["preconditions"])
            and _is_text_list(step["verification"]),
            f"manifest_step_{step['id']}_invalid",
        )
        if step["scope"] == "separate_approval":
            _require(
                "separate owner approval" in step["approval"],
                f"manifest_step_{step['id']}_approval_not_separate",
            )
    blocks = document["known_blocks"]
    _require(
        isinstance(blocks, list)
        and all(
            isinstance(row, dict)
            and set(row) == {"id", "blocks", "reason"}
            and isinstance(row["reason"], str)
            for row in blocks
        )
        and {row["id"]: row["blocks"] for row in blocks} == KNOWN_BLOCKS
        and len(blocks) == len(KNOWN_BLOCKS),
        "manifest_known_blocks_mismatch",
    )
    serialized = _canonical(document)
    _require(
        serialized.count("ABACATEPAY") == 1, "manifest_abacatepay_outside_forbidden"
    )
    _validate_migrations_window(document, serialized)
    return document


def _validate_migrations_window(document: dict[str, Any], serialized: str) -> None:
    """The 033-035 window names exactly ``alembic upgrade 035``, nothing wider."""

    step = next(row for row in document["steps"] if row["id"] == MIGRATIONS_STEP)
    command = "`" + " ".join(MIGRATIONS_COMMAND) + "`"
    _require(
        _UNPINNED_UPGRADE.search(serialized) is None
        and _ALEMBIC_UPGRADE.findall(serialized) == [MIGRATIONS_TARGET_REVISION]
        and command in step["change"],
        "manifest_migrations_window_not_pinned_to_035",
    )
    preconditions = "\n".join(step["preconditions"])
    verification = "\n".join(step["verification"])
    _require(
        _ALEMBIC_VERSION.findall(preconditions) == [MIGRATIONS_SOURCE_REVISION]
        and MIGRATIONS_VERIFICATION in verification
        and _ALEMBIC_VERSION.findall(verification) == [MIGRATIONS_TARGET_REVISION],
        "manifest_migrations_window_readback_not_pinned",
    )


def validate_parameters(raw: Any) -> dict[str, Any]:
    """Return the reviewed parameters or fail the plan on any missing/invalid one."""

    _require(isinstance(raw, dict), "parameters_file_invalid")
    assert isinstance(raw, dict)
    _require(
        set(raw) == {"schema_version", "parameters"}
        and raw.get("schema_version") == PARAMETERS_SCHEMA,
        "parameters_schema_invalid",
    )
    values = raw["parameters"]
    _require(isinstance(values, dict), "parameters_invalid")
    allowed = set(REQUIRED_PARAMETERS) | {IMAGE_PARAMETER}
    unknown = sorted(set(values) - allowed)
    _require(not unknown, f"parameter_unknown:{','.join(unknown)}")
    output: dict[str, Any] = {}
    for name in REQUIRED_PARAMETERS:
        value = values.get(name)
        _require(
            isinstance(value, str) and bool(value.strip()), f"parameter_missing:{name}"
        )
        assert isinstance(value, str)
        _require(value == value.strip(), f"parameter_invalid:{name}")
        output[name] = value
    _validate_public_url(
        output[RETURN_URL_PARAMETER],
        RETURN_URL_PARAMETER,
        RETURN_PATH,
        Settings.validate_asaas_checkout_return_url,
    )
    _validate_public_url(
        output[WEBHOOK_URL_PARAMETER],
        WEBHOOK_URL_PARAMETER,
        WEBHOOK_PATH,
        Settings.validate_asaas_webhook_url,
    )
    email = output[EMAIL_PARAMETER]
    _require(
        email.isascii()
        and len(email) <= 254
        and _OPERATOR_EMAIL.fullmatch(email) is not None,
        f"parameter_invalid:{EMAIL_PARAMETER}",
    )
    image = values.get(IMAGE_PARAMETER)
    _require(
        image is None
        or (
            isinstance(image, str)
            and _ECR_DIGEST_REFERENCE.fullmatch(image) is not None
        ),
        f"parameter_invalid:{IMAGE_PARAMETER}",
    )
    output[IMAGE_PARAMETER] = image
    return output


def _validate_public_url(
    value: str, name: str, path: str, validator: Callable[[str], str]
) -> None:
    try:
        accepted = validator(value)
    except ValueError:
        raise WiringError(f"parameter_invalid:{name}") from None
    _require(accepted == value, f"parameter_invalid:{name}")
    parsed = urlsplit(value)
    _require(
        parsed.scheme == "https"
        and parsed.port is None
        and parsed.hostname is not None
        and parsed.netloc == parsed.hostname
        and parsed.hostname in SERVED_HOSTS
        and parsed.path == path,
        f"parameter_invalid:{name}",
    )


def validate_secret_access_template(template: Any) -> dict[str, Any]:
    """Pin the reviewed CloudFormation template exactly."""

    document = _exact_keys(
        template,
        {
            "AWSTemplateFormatVersion",
            "Description",
            "Metadata",
            "Rules",
            "Parameters",
            "Resources",
            "Outputs",
        },
        "template",
    )
    _require(
        document["AWSTemplateFormatVersion"] == "2010-09-09"
        and isinstance(document["Description"], str),
        "template_header_invalid",
    )
    metadata = _exact_keys(
        document["Metadata"], {"com.aws.cloudformation.Context"}, "template_metadata"
    )
    context = _exact_keys(
        metadata["com.aws.cloudformation.Context"],
        {"arch", "must", "ref"},
        "template_context",
    )
    _require(_is_text_list(context["must"]), "template_context_invalid")
    _require(
        document["Rules"]
        == {
            "ProductionServingAccountRegionBoundary": {
                "Assertions": [
                    {
                        "Assert": {
                            "Fn::Equals": [{"Ref": "AWS::AccountId"}, ACCOUNT_ID]
                        },
                        "AssertDescription": (
                            "Wiring resources belong only to the verified AWS account."
                        ),
                    },
                    {
                        "Assert": {"Fn::Equals": [{"Ref": "AWS::Region"}, REGION]},
                        "AssertDescription": (
                            "Wiring resources belong only to the verified AWS region."
                        ),
                    },
                ]
            }
        },
        "template_account_region_rule_invalid",
    )
    parameters = _exact_keys(
        document["Parameters"],
        {"AsaasApiKeySecretArn", "AsaasWebhookTokenSecretArn"},
        "template_parameters",
    )
    for env, name, source, parameter_name in SECRET_REFERENCES:
        if source != "existing":
            continue
        assert parameter_name is not None
        parameter = _exact_keys(
            parameters[parameter_name],
            {"Type", "Description", "AllowedPattern", "ConstraintDescription"},
            f"template_parameter_{parameter_name}",
        )
        _require(
            parameter["Type"] == "String"
            and parameter["AllowedPattern"]
            == (
                f"^arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:"
                f"{name}-[A-Za-z0-9]{{6}}$"
            ),
            f"template_parameter_{parameter_name}_invalid",
        )
    resources = _exact_keys(
        document["Resources"],
        {"BillingObservabilitySmokeTokenSecret", "ExecutionRoleWiringSecretRead"},
        "template_resources",
    )
    secret = _exact_keys(
        resources["BillingObservabilitySmokeTokenSecret"],
        {"Type", "DeletionPolicy", "UpdateReplacePolicy", "Metadata", "Properties"},
        "template_secret",
    )
    _require(
        secret["Type"] == "AWS::SecretsManager::Secret"
        and secret["DeletionPolicy"] == "Retain"
        and secret["UpdateReplacePolicy"] == "Retain",
        "template_secret_policy_invalid",
    )
    properties = _exact_keys(
        secret["Properties"],
        {"Name", "Description", "KmsKeyId", "GenerateSecretString", "Tags"},
        "template_secret_properties",
    )
    _require(
        properties["Name"] == SECRET_REFERENCES[2][1]
        and properties["KmsKeyId"] == KMS_KEY_ARN
        and properties["GenerateSecretString"]
        == {
            "PasswordLength": 64,
            "ExcludePunctuation": True,
            "IncludeSpace": False,
            "RequireEachIncludedType": True,
        },
        "template_secret_properties_invalid",
    )
    tags = properties["Tags"]
    _require(
        isinstance(tags, list)
        and all(
            isinstance(tag, dict)
            and set(tag) == {"Key", "Value"}
            and all(isinstance(item, str) for item in tag.values())
            for tag in tags
        ),
        "template_secret_tags_invalid",
    )
    tag_map = {tag["Key"]: tag["Value"] for tag in tags}
    _require(
        len(tag_map) == len(tags)
        and tag_map.get("ManagedBy") == "cloudformation"
        and tag_map.get("Environment") == "staging"
        and tag_map.get("DataClassification") == "confidential",
        "template_secret_tags_invalid",
    )
    policy = _exact_keys(
        resources["ExecutionRoleWiringSecretRead"],
        {"Type", "Metadata", "Properties"},
        "template_policy",
    )
    _require(policy["Type"] == "AWS::IAM::Policy", "template_policy_type_invalid")
    policy_properties = _exact_keys(
        policy["Properties"],
        {"PolicyName", "Roles", "PolicyDocument"},
        "template_policy_properties",
    )
    _require(
        policy_properties["PolicyName"] == WIRING_POLICY_NAME
        and policy_properties["Roles"] == [EXECUTION_ROLE_NAME],
        "template_policy_target_invalid",
    )
    _require(
        policy_properties["PolicyDocument"]
        == {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "ReadOnlyTheThreeWiringSecrets",
                    "Effect": "Allow",
                    "Action": GET_SECRET_VALUE,
                    "Resource": [
                        {"Ref": "AsaasApiKeySecretArn"},
                        {"Ref": "AsaasWebhookTokenSecretArn"},
                        {"Ref": "BillingObservabilitySmokeTokenSecret"},
                    ],
                }
            ],
        },
        "template_policy_document_invalid",
    )
    outputs = _exact_keys(
        document["Outputs"],
        {"BillingObservabilitySmokeTokenSecretArn", "ExecutionRolePolicyName"},
        "template_outputs",
    )
    for output_name, value in (
        (
            "BillingObservabilitySmokeTokenSecretArn",
            {"Ref": "BillingObservabilitySmokeTokenSecret"},
        ),
        ("ExecutionRolePolicyName", WIRING_POLICY_NAME),
    ):
        output = _exact_keys(
            outputs[output_name],
            {"Description", "Value"},
            f"template_output_{output_name}",
        )
        _require(
            isinstance(output["Description"], str) and output["Value"] == value,
            "template_outputs_invalid",
        )
    serialized = _canonical(document)
    _require(
        "resolve:secretsmanager" not in serialized
        and '"SecretString"' not in serialized
        and "ABACATEPAY" not in serialized.upper(),
        "template_secret_material_or_legacy_name",
    )
    return document


def load_inputs(
    manifest_path: Path, parameters_path: Path | None
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, str]]:
    manifest = validate_manifest(load_yaml(manifest_path))
    template_path = _REPOSITORY_ROOT / TEMPLATE_RELATIVE_PATH
    validate_secret_access_template(load_yaml(template_path))
    digests = {
        "manifest_sha256": _file_sha256(manifest_path),
        "template_sha256": _file_sha256(template_path),
    }
    parameters = None
    if parameters_path is not None:
        parameters = validate_parameters(load_yaml(parameters_path))
        digests["parameters_sha256"] = _file_sha256(parameters_path)
    return manifest, parameters, digests


# ---------------------------------------------------------------------------
# Readback collection (read-only, deterministic requests)
# ---------------------------------------------------------------------------


def _image_lookup_params(image: str) -> dict[str, Any] | None:
    match = _ECR_IMAGE.fullmatch(image)
    if match is None or not (match.group("tag") or match.group("digest")):
        return None
    image_id = (
        {"imageDigest": match.group("digest")}
        if match.group("digest")
        else {"imageTag": match.group("tag")}
    )
    return {"repositoryName": match.group("repository"), "imageIds": [image_id]}


def collect_readback(
    aws: ReadOnlyAws, parameters: dict[str, Any] | None, *, pins: ManifestPins
) -> dict[str, Any]:
    """Issue the fixed set of read-only requests the plan needs."""

    readback: dict[str, Any] = {
        "caller": aws.call("sts", "GetCallerIdentity", {}),
        "service": aws.call(
            "ecs", "DescribeServices", {"cluster": CLUSTER, "services": [SERVICE]}
        ),
        "source_task_definition": aws.call(
            "ecs",
            "DescribeTaskDefinition",
            {
                "taskDefinition": f"{TASK_FAMILY}:{D3_SOURCE_REVISION}",
                "include": ["TAGS"],
            },
        ),
        "rollback_task_definition": (
            aws.call(
                "ecs",
                "DescribeTaskDefinition",
                {
                    "taskDefinition": f"{TASK_FAMILY}:{pins.rollback_revision}",
                    "include": ["TAGS"],
                },
            )
            if pins.rollback_revision != D3_SOURCE_REVISION
            else None
        ),
        "latest_task_definition": aws.call(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": TASK_FAMILY, "include": ["TAGS"]},
        ),
        "execution_role": aws.call("iam", "GetRole", {"RoleName": EXECUTION_ROLE_NAME}),
        "execution_inline_policies": aws.call(
            "iam", "ListRolePolicies", {"RoleName": EXECUTION_ROLE_NAME}
        ),
        "execution_attached_policies": aws.call(
            "iam", "ListAttachedRolePolicies", {"RoleName": EXECUTION_ROLE_NAME}
        ),
        "terraform_policy": aws.call(
            "iam",
            "GetRolePolicy",
            {"RoleName": EXECUTION_ROLE_NAME, "PolicyName": TERRAFORM_POLICY_NAME},
        ),
        "wiring_policy": aws.call(
            "iam",
            "GetRolePolicy",
            {"RoleName": EXECUTION_ROLE_NAME, "PolicyName": WIRING_POLICY_NAME},
        ),
        "task_inline_policies": aws.call(
            "iam", "ListRolePolicies", {"RoleName": TASK_ROLE_NAME}
        ),
        "task_attached_policies": aws.call(
            "iam", "ListAttachedRolePolicies", {"RoleName": TASK_ROLE_NAME}
        ),
        "secrets": {
            env: aws.call("secretsmanager", "DescribeSecret", {"SecretId": name})
            for env, name, _source, _parameter in SECRET_REFERENCES
        },
        "stack": aws.call(
            "cloudformation", "DescribeStacks", {"StackName": STACK_NAME}
        ),
        "appconfig_applications": aws.call("appconfig", "ListApplications", {}),
    }
    stack_exists = _error_code(readback["stack"]) is None
    readback["stack_resources"] = (
        aws.call("cloudformation", "ListStackResources", {"StackName": STACK_NAME})
        if stack_exists
        else None
    )
    readback["stack_template"] = (
        aws.call(
            "cloudformation",
            "GetTemplate",
            {"StackName": STACK_NAME, "TemplateStage": "Original"},
        )
        if stack_exists
        else None
    )
    app = _source_app_container(readback["source_task_definition"])
    live_image = app.get("image") if isinstance(app, dict) else None
    lookup = _image_lookup_params(live_image) if isinstance(live_image, str) else None
    readback["live_image"] = (
        aws.call("ecr", "DescribeImages", lookup) if lookup is not None else None
    )
    override = parameters.get(IMAGE_PARAMETER) if parameters else None
    override_lookup = _image_lookup_params(override) if override else None
    readback["override_image"] = (
        aws.call("ecr", "DescribeImages", override_lookup)
        if override_lookup is not None
        else None
    )
    load_balancers = aws.call(
        "elbv2", "DescribeLoadBalancers", {"Names": [LOAD_BALANCER_NAME]}
    )
    readback["load_balancers"] = load_balancers
    rows = load_balancers.get("LoadBalancers")
    lb_arn = (
        rows[0].get("LoadBalancerArn")
        if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
        else None
    )
    readback["listeners"] = None
    readback["https_rules"] = None
    readback["certificate"] = None
    readback["web_acl"] = None
    if isinstance(lb_arn, str):
        listeners = aws.call("elbv2", "DescribeListeners", {"LoadBalancerArn": lb_arn})
        readback["listeners"] = listeners
        https = _https_listener(listeners)
        if https is not None:
            readback["https_rules"] = aws.call(
                "elbv2", "DescribeRules", {"ListenerArn": https["ListenerArn"]}
            )
            certificates = https.get("Certificates")
            if (
                isinstance(certificates, list)
                and len(certificates) == 1
                and isinstance(certificates[0].get("CertificateArn"), str)
            ):
                readback["certificate"] = aws.call(
                    "acm",
                    "DescribeCertificate",
                    {"CertificateArn": certificates[0]["CertificateArn"]},
                )
        readback["web_acl"] = aws.call(
            "wafv2", "GetWebACLForResource", {"ResourceArn": lb_arn}
        )
    zones = aws.call("route53", "ListHostedZonesByName", {"DNSName": HOSTED_ZONE_NAME})
    readback["hosted_zones"] = zones
    zone_id = _hosted_zone_id(zones)
    readback["record_sets"] = (
        aws.call("route53", "ListResourceRecordSets", {"HostedZoneId": zone_id})
        if zone_id is not None
        else None
    )
    return readback


def _source_app_container(response: Any) -> dict[str, Any] | None:
    definition = response.get("taskDefinition") if isinstance(response, dict) else None
    containers = (
        definition.get("containerDefinitions") if isinstance(definition, dict) else None
    )
    if not isinstance(containers, list):
        return None
    apps = [c for c in containers if isinstance(c, dict) and c.get("name") == "app"]
    return apps[0] if len(apps) == 1 else None


def _https_listener(listeners: Any) -> dict[str, Any] | None:
    rows = listeners.get("Listeners") if isinstance(listeners, dict) else None
    if not isinstance(rows, list):
        return None
    https = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("Port") == 443
        and row.get("Protocol") == "HTTPS"
        and isinstance(row.get("ListenerArn"), str)
    ]
    return https[0] if len(https) == 1 else None


def _dns(value: Any) -> str:
    return str(value or "").lower().rstrip(".")


def _hosted_zone_id(zones: Any) -> str | None:
    rows = zones.get("HostedZones") if isinstance(zones, dict) else None
    if not isinstance(rows, list):
        return None
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and _dns(row.get("Name")) == HOSTED_ZONE_NAME
        and not (row.get("Config") or {}).get("PrivateZone", False)
        and isinstance(row.get("Id"), str)
    ]
    if len(matches) != 1:
        return None
    return str(matches[0]["Id"]).removeprefix("/hostedzone/")


# ---------------------------------------------------------------------------
# Task definition rendering
# ---------------------------------------------------------------------------


def _request_from_response(response: Any) -> dict[str, Any]:
    _require(
        isinstance(response, dict) and isinstance(response.get("taskDefinition"), dict),
        "task_definition_readback_invalid",
    )
    definition = response["taskDefinition"]
    request = {
        key: copy.deepcopy(value)
        for key, value in definition.items()
        if key not in evidence._RESPONSE_ONLY_FIELDS
    }
    tags = response.get("tags")
    if isinstance(tags, list) and tags:
        request["tags"] = copy.deepcopy(tags)
    _require(
        set(request) <= evidence._EXPECTED_DEFINITION_FIELDS,
        "source_task_definition_field_unexpected",
    )
    return request


def _names(container: dict[str, Any], field: str) -> list[str]:
    rows = container.get(field, [])
    _require(isinstance(rows, list), f"task_definition_{field}_invalid")
    names = [row.get("name") for row in rows if isinstance(row, dict)]
    _require(
        len(names) == len(rows) and all(isinstance(name, str) for name in names),
        f"task_definition_{field}_invalid",
    )
    return [str(name) for name in names]


def forbidden_names_in(request: dict[str, Any]) -> list[str]:
    """Every forbidden env/secret name in any container (empty when clean)."""

    containers = request.get("containerDefinitions")
    _require(isinstance(containers, list), "task_definition_containers_invalid")
    assert isinstance(containers, list)
    found: list[str] = []
    for container in containers:
        _require(isinstance(container, dict), "task_definition_container_invalid")
        for field in ("environment", "secrets"):
            for name in _names(container, field):
                if name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_NAME_PREFIXES):
                    found.append(name)
    return sorted(found)


def apply_tag_overrides(
    request: dict[str, Any], overrides: tuple[tuple[str, str], ...]
) -> None:
    """Set each override tag in place (or append it); keep every other tag."""

    tags = request.get("tags", [])
    _require(
        isinstance(tags, list)
        and all(
            isinstance(tag, dict)
            and set(tag) == {"key", "value"}
            and all(isinstance(item, str) for item in tag.values())
            for tag in tags
        )
        and len({tag["key"] for tag in tags}) == len(tags),
        "task_definition_tags_invalid",
    )
    wanted = dict(overrides)
    output = [
        {"key": tag["key"], "value": wanted.get(tag["key"], tag["value"])}
        for tag in tags
    ]
    present = {tag["key"] for tag in tags}
    output.extend(
        {"key": key, "value": value} for key, value in overrides if key not in present
    )
    request["tags"] = output


def render_task_definition(
    source_response: Any,
    *,
    secret_arns: dict[str, str],
    parameters: dict[str, Any],
    pins: ManifestPins,
) -> dict[str, Any]:
    """Derive the new revision request: live source plus exactly the D3 names.

    Besides the D3 names only two things change: the otel image becomes the
    manifest's digest pin and the tags carry ``ManagedBy=manual``.
    """

    _require(
        _OTEL_IMAGE.fullmatch(pins.otel_image) is not None,
        "manifest_otel_image_not_digest",
    )
    request = _request_from_response(source_response)
    try:
        containers = evidence._container_map(request)
    except evidence.EvidenceError as exc:
        raise WiringError(f"source_{exc}") from None
    app = containers[APP_CONTAINER]
    env_names = _names(app, "environment")
    secret_names = _names(app, "secrets")
    _require(
        not (set(ADDED_ENVIRONMENT) | set(ADDED_SECRETS))
        & (set(env_names) | set(secret_names)),
        "source_already_has_added_name",
    )
    _require(set(secret_arns) == set(ADDED_SECRETS), "secret_reference_set_mismatch")
    for name, arn in secret_arns.items():
        _require(
            isinstance(arn, str)
            and evidence._SECRET_ARN.fullmatch(arn) is not None
            and _SECRET_ARN_SUFFIX.search(arn) is not None,
            f"secret_reference_invalid:{name}",
        )
    environment = list(app.get("environment", []))
    for name in sorted(ADDED_ENVIRONMENT):
        kind, reference = ADDED_ENVIRONMENT[name]
        value = parameters[reference] if kind == "parameter" else reference
        _require(isinstance(value, str) and value != "", f"parameter_missing:{name}")
        environment.append({"name": name, "value": value})
    app["environment"] = environment
    secrets = list(app.get("secrets", []))
    for name in sorted(ADDED_SECRETS):
        secrets.append({"name": name, "valueFrom": secret_arns[name]})
    app["secrets"] = secrets
    override = parameters.get(IMAGE_PARAMETER)
    if override is not None:
        app["image"] = override
    containers[OTEL_CONTAINER]["image"] = pins.otel_image
    apply_tag_overrides(request, pins.tag_overrides)
    forbidden = forbidden_names_in(request)
    _require(not forbidden, "forbidden_name_in_rendering:" + ",".join(forbidden))
    return request


def _migrations_command_pinned(command: tuple[str, ...]) -> bool:
    return (
        _PINNED_ALEMBIC_REVISION.fullmatch(MIGRATIONS_TARGET_REVISION) is not None
        and tuple(MIGRATIONS_COMMAND)
        == ("alembic", "upgrade", MIGRATIONS_TARGET_REVISION)
        and tuple(command) == tuple(MIGRATIONS_COMMAND)
    )


def _without_fault_injection(response: Any) -> Any:
    """Drop ``enableFaultInjection`` when false; refuse it when true.

    Newer DescribeTaskDefinition responses carry the key. Only ``false`` (the
    default) is reproduced by omitting it from a registration request.
    """

    definition = response.get("taskDefinition") if isinstance(response, dict) else None
    if not isinstance(definition, dict) or "enableFaultInjection" not in definition:
        return response
    _require(
        definition["enableFaultInjection"] is False,
        "migrations_source_fault_injection_not_false",
    )
    cleaned = copy.deepcopy(response)
    cleaned["taskDefinition"].pop("enableFaultInjection")
    return cleaned


def render_migrations_task_definition(
    source_response: Any,
    *,
    image: str,
    pins: ManifestPins,
    command: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """A new migrations revision: revision 3 with the image and command replaced.

    The command is baked into the revision (no override at run time) and must be
    exactly ``MIGRATIONS_COMMAND``; anything else is refused. Roles, the
    ``database-url-migrations`` secret, sizing and logs come from revision 3,
    which is only read and never run.
    """

    _require(
        _migrations_command_pinned(MIGRATIONS_COMMAND if command is None else command),
        "migrations_command_not_pinned",
    )
    definition = (
        source_response.get("taskDefinition")
        if isinstance(source_response, dict)
        else None
    )
    _require(
        isinstance(definition, dict)
        and definition.get("taskDefinitionArn") == MIGRATIONS_TASK_SOURCE_ARN
        and definition.get("family") == MIGRATIONS_FAMILY
        and definition.get("revision") == MIGRATIONS_TASK_SOURCE_REVISION,
        "migrations_source_revision_mismatch",
    )
    assert isinstance(definition, dict)
    _require(definition.get("status") == "ACTIVE", "migrations_source_not_active")
    _require(
        definition.get("executionRoleArn") == evidence.EXECUTION_ROLE_ARN
        and definition.get("taskRoleArn", evidence.TASK_ROLE_ARN)
        == evidence.TASK_ROLE_ARN,
        "migrations_source_roles_mismatch",
    )
    _require(
        isinstance(image, str) and _ECR_DIGEST_REFERENCE.fullmatch(image) is not None,
        "migrations_image_not_digest_pinned",
    )
    request = _request_from_response(_without_fault_injection(source_response))
    containers = request.get("containerDefinitions")
    _require(
        isinstance(containers, list)
        and len(containers) == 1
        and isinstance(containers[0], dict),
        "migrations_source_container_count_invalid",
    )
    assert isinstance(containers, list)
    container = containers[0]
    _require(
        not container.get("entryPoint"), "migrations_source_entry_point_unexpected"
    )
    try:
        environment = evidence._env_map(container)
        secrets = evidence._secret_map(container)
    except evidence.EvidenceError as exc:
        raise WiringError(f"migrations_source_{exc}") from None
    _require(
        "DATABASE_URL" not in environment
        and _MIGRATIONS_DATABASE_SECRET_ARN.fullmatch(secrets.get("DATABASE_URL", ""))
        is not None,
        "migrations_database_secret_mismatch",
    )
    container["image"] = image
    container["command"] = list(MIGRATIONS_COMMAND)
    apply_tag_overrides(request, pins.tag_overrides)
    forbidden = forbidden_names_in(request)
    _require(not forbidden, "forbidden_name_in_rendering:" + ",".join(forbidden))
    return request


def collect_migrations_readback(
    aws: ReadOnlyAws, parameters: dict[str, Any]
) -> dict[str, Any]:
    """The three reads the migrations revision needs (all read-only)."""

    image = parameters.get(IMAGE_PARAMETER)
    _require(isinstance(image, str), "migrations_image_requires_app_image_override")
    assert isinstance(image, str)
    lookup = _image_lookup_params(image)
    _require(lookup is not None, "migrations_image_not_digest_pinned")
    assert lookup is not None
    return {
        "source": aws.call(
            "ecs",
            "DescribeTaskDefinition",
            {
                "taskDefinition": f"{MIGRATIONS_FAMILY}:{MIGRATIONS_TASK_SOURCE_REVISION}",
                "include": ["TAGS"],
            },
        ),
        "latest": aws.call(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": MIGRATIONS_FAMILY, "include": ["TAGS"]},
        ),
        "image": aws.call("ecr", "DescribeImages", lookup),
    }


def plan_migrations(
    readback: dict[str, Any], parameters: dict[str, Any], *, pins: ManifestPins
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Status of the migrations revision plus the request, None when blocked."""

    image = parameters[IMAGE_PARAMETER]
    status: dict[str, Any] = {
        "family": MIGRATIONS_FAMILY,
        "read_only_source": MIGRATIONS_TASK_SOURCE_ARN,
        "command": " ".join(MIGRATIONS_COMMAND),
        "blocks": [],
        "state": None,
        "run_task_definition": None,
    }
    rows = (readback.get("image") or {}).get("imageDetails")
    match = _ECR_IMAGE.fullmatch(image) if isinstance(image, str) else None
    if not (
        isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and match is not None
        and rows[0].get("imageDigest") == match.group("digest")
    ):
        status["blocks"].append("migrations_image_not_found_in_ecr")
    try:
        rendered = render_migrations_task_definition(
            readback.get("source"), image=image, pins=pins
        )
    except WiringError as exc:
        status["blocks"].append(str(exc))
        return status, None
    latest_response = readback.get("latest")
    latest = (
        latest_response.get("taskDefinition")
        if isinstance(latest_response, dict)
        else None
    )
    latest_arn = latest.get("taskDefinitionArn") if isinstance(latest, dict) else None
    if not _is_migrations_revision(latest, latest_arn):
        # Never tell the operator to run a revision of another account,
        # region or family, or one that is not at least revision 3.
        status["blocks"].append("migrations_newest_revision_invalid")
    elif latest_arn == MIGRATIONS_TASK_SOURCE_ARN:
        status["state"] = "would_register"
    else:
        try:
            same = _comparable(
                _request_from_response(_without_fault_injection(latest_response))
            ) == _comparable(rendered)
        except WiringError:
            same = False
        if same and isinstance(latest_arn, str):
            status["state"] = f"registered:{latest_arn}"
            status["run_task_definition"] = latest_arn
        else:
            status["blocks"].append(f"migrations_newer_revision_differs:{latest_arn}")
    if status["blocks"]:
        return status, None
    return status, rendered


_MIGRATIONS_REVISION_ARN = re.compile(
    rf"^arn:aws:ecs:{re.escape(REGION)}:{ACCOUNT_ID}:task-definition/"
    rf"{re.escape(MIGRATIONS_FAMILY)}:(?P<revision>[1-9][0-9]*)$"
)


def _is_migrations_revision(definition: Any, arn: Any) -> bool:
    """An ARN of this family in this account and region, revision >= 3."""

    match = _MIGRATIONS_REVISION_ARN.fullmatch(arn) if isinstance(arn, str) else None
    return (
        match is not None
        and isinstance(definition, dict)
        and definition.get("family") == MIGRATIONS_FAMILY
        and int(match.group("revision")) >= MIGRATIONS_TASK_SOURCE_REVISION
        and definition.get("revision") == int(match.group("revision"))
    )


def _comparable(request: dict[str, Any]) -> str:
    normalized = copy.deepcopy(request)
    containers = normalized.get("containerDefinitions")
    if isinstance(containers, list):
        for container in containers:
            if not isinstance(container, dict):
                continue
            for field in ("environment", "secrets"):
                rows = container.get(field)
                if isinstance(rows, list):
                    container[field] = sorted(
                        rows, key=lambda row: str(row.get("name", ""))
                    )
                elif field in container and rows is None:
                    container[field] = []
        normalized["containerDefinitions"] = sorted(
            (c for c in containers if isinstance(c, dict)),
            key=lambda c: str(c.get("name", "")),
        )
    tags = normalized.get("tags")
    if isinstance(tags, list):
        normalized["tags"] = sorted(tags, key=lambda tag: str(tag.get("key", "")))
    return _canonical(normalized)


@contextlib.contextmanager
def _synthetic_trust_pins() -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in _SYNTHETIC_TRUST_PINS}
    os.environ.update(_SYNTHETIC_TRUST_PINS)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def group5_template_verdict(
    request: dict[str, Any], source_arn: str
) -> tuple[bool, str]:
    """Run the protected deploy path's template validator on a request."""

    template = {
        "schema_version": evidence.TASK_DEFINITION_TEMPLATE_SCHEMA,
        "source_task_definition_arn": source_arn,
        "task_definition": copy.deepcopy(request),
    }
    with _synthetic_trust_pins():
        try:
            evidence.validate_task_definition_template(template)
        except evidence.EvidenceError as exc:
            return False, str(exc)
    return True, "pass"


def run_evidence_validators(
    request: dict[str, Any], source_arn: str
) -> list[dict[str, Any]]:
    """Apply the existing rollout evidence validators to a rendered request."""

    results: list[dict[str, Any]] = []

    def record(name: str, scope: str, check: Callable[[], None]) -> None:
        try:
            check()
        except (
            evidence.EvidenceError,
            rollout.RolloutError,
            WiringError,
            KeyError,
            TypeError,
        ) as exc:
            results.append(
                {
                    "validator": name,
                    "scope": scope,
                    "result": "fail",
                    "reason": str(exc),
                }
            )
            return
        results.append({"validator": name, "scope": scope, "result": "pass"})

    def containers() -> None:
        evidence._container_map(request)

    def otel_environment() -> None:
        otel = evidence._container_map(request)["otel"]
        _require(evidence._env_map(otel) == {}, "otel_environment_must_be_empty")

    def otel_secrets() -> None:
        otel = evidence._container_map(request)["otel"]
        _require(evidence._secret_map(otel) == {}, "otel_secrets_must_be_empty")

    def app_environment() -> None:
        evidence._env_map(evidence._container_map(request)[APP_CONTAINER])

    def app_secret_references() -> None:
        evidence._secret_map(evidence._container_map(request)[APP_CONTAINER])

    def no_secret_value_fields() -> None:
        evidence._reject_sensitive_value_fields(request)

    def rollout_environment() -> None:
        rollout._environment_map(request)

    def database_reference() -> None:
        rollout._database_reference(request)

    def runtime_fingerprint() -> None:
        rollout._task_definition_runtime_fingerprint(request)

    def secrets_only_as_references() -> None:
        app = evidence._container_map(request)[APP_CONTAINER]
        env = evidence._env_map(app)
        secrets = evidence._secret_map(app)
        _require(
            not set(ADDED_SECRETS) & set(env)
            and set(ADDED_SECRETS) <= set(secrets)
            and all(
                evidence._SECRET_ARN.fullmatch(":".join(ref.split(":")[:7]))
                for ref in secrets.values()
            ),
            "secret_not_a_reference",
        )

    def no_forbidden_names() -> None:
        found = forbidden_names_in(request)
        _require(not found, "forbidden_name:" + ",".join(found))

    def group5_template() -> None:
        passed, reason = group5_template_verdict(request, source_arn)
        _require(passed, reason)

    record("asaas_producer_rollout_evidence._container_map", "structural", containers)
    record(
        "asaas_producer_rollout_evidence.otel_environment",
        "structural",
        otel_environment,
    )
    record("asaas_producer_rollout_evidence.otel_secrets", "structural", otel_secrets)
    record(
        "asaas_producer_rollout_evidence._env_map(app)", "structural", app_environment
    )
    record(
        "asaas_producer_rollout_evidence._secret_map(app)",
        "structural",
        app_secret_references,
    )
    record(
        "asaas_producer_rollout_evidence._reject_sensitive_value_fields",
        "structural",
        no_secret_value_fields,
    )
    record(
        "verify_asaas_producer_image_rollout._environment_map",
        "structural",
        rollout_environment,
    )
    record(
        "verify_asaas_producer_image_rollout._database_reference",
        "structural",
        database_reference,
    )
    record(
        "verify_asaas_producer_image_rollout._task_definition_runtime_fingerprint",
        "structural",
        runtime_fingerprint,
    )
    record(
        "wiring.secrets_only_as_references", "structural", secrets_only_as_references
    )
    record("wiring.no_forbidden_names", "structural", no_forbidden_names)
    record(
        "asaas_producer_rollout_evidence.validate_task_definition_template",
        "group5_deploy_path",
        group5_template,
    )
    return results


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class _Step:
    def __init__(self, step_id: str, scope: str) -> None:
        self.id = step_id
        self.scope = scope
        self.changes: list[dict[str, Any]] = []
        self.compliant: list[str] = []
        self.blocks: list[str] = []
        self.pending: list[str] = []
        self.info: dict[str, Any] = {}

    def state(self) -> str:
        if self.blocks:
            return "blocked"
        if self.scope == "separate_approval":
            return "not_performed"
        if self.pending:
            return "pending_dependency"
        if self.changes:
            return "would_change"
        return "compliant"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "state": self.state(),
            "changes": self.changes,
            "compliant": self.compliant,
            "blocks": self.blocks,
            "pending": self.pending,
            "info": self.info,
        }


def _policy_document(response: Any) -> Any:
    document = response.get("PolicyDocument") if isinstance(response, dict) else None
    if isinstance(document, str):
        try:
            return json.loads(unquote(document))
        except ValueError:
            return None
    return document


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def expected_wiring_policy(secret_arns: list[str]) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadOnlyTheThreeWiringSecrets",
                "Effect": "Allow",
                "Action": GET_SECRET_VALUE,
                "Resource": list(secret_arns),
            }
        ],
    }


def wiring_policy_problems(document: Any, expected_arns: set[str]) -> list[str]:
    """Exactly one Allow of GetSecretValue on exactly the three ARNs."""

    if not isinstance(document, dict) or set(document) != {"Version", "Statement"}:
        return ["policy_document_shape"]
    if document["Version"] != "2012-10-17":
        return ["policy_version"]
    statements = _as_list(document["Statement"])
    if len(statements) != 1 or not isinstance(statements[0], dict):
        return ["policy_statement_count"]
    statement = statements[0]
    problems: list[str] = []
    if set(statement) - {"Sid"} != {"Effect", "Action", "Resource"}:
        problems.append("policy_statement_keys")
    if statement.get("Effect") != "Allow":
        problems.append("policy_effect")
    if _as_list(statement.get("Action")) != [GET_SECRET_VALUE]:
        problems.append("policy_action")
    resources = _as_list(statement.get("Resource"))
    if any(
        not isinstance(item, str) or "*" in item or "?" in item for item in resources
    ):
        problems.append("policy_wildcard_resource")
    elif len(resources) != len(set(resources)) or set(resources) != expected_arns:
        problems.append("policy_resources")
    return problems


def _decrypt_granted(document: Any) -> bool:
    if not isinstance(document, dict):
        return False
    for statement in _as_list(document.get("Statement")):
        if (
            isinstance(statement, dict)
            and statement.get("Effect") == "Allow"
            and "kms:Decrypt" in _as_list(statement.get("Action"))
            and KMS_KEY_ARN in _as_list(statement.get("Resource"))
            and "Condition" not in statement
        ):
            return True
    return False


def _granted_secret_arns(document: Any) -> set[str]:
    granted: set[str] = set()
    if not isinstance(document, dict):
        return granted
    for statement in _as_list(document.get("Statement")):
        if (
            isinstance(statement, dict)
            and statement.get("Effect") == "Allow"
            and GET_SECRET_VALUE in _as_list(statement.get("Action"))
        ):
            granted |= {
                item
                for item in _as_list(statement.get("Resource"))
                if isinstance(item, str)
            }
    return granted


def _secret_problems(response: Any, name: str) -> tuple[str | None, list[str]]:
    code = _error_code(response)
    if code == "ResourceNotFoundException":
        return None, ["absent"]
    if code is not None or not isinstance(response, dict):
        return None, ["unreadable"]
    arn = response.get("ARN")
    problems: list[str] = []
    if not (
        isinstance(arn, str)
        and arn.startswith(
            f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:{name}-"
        )
        and _SECRET_ARN_SUFFIX.search(arn)
        and len(arn)
        == len(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:{name}") + 7
    ):
        problems.append("arn_unexpected")
    if response.get("Name") != name:
        problems.append("name_mismatch")
    if response.get("DeletedDate"):
        problems.append("scheduled_for_deletion")
    if response.get("KmsKeyId") != KMS_KEY_ARN:
        problems.append("kms_key_mismatch")
    stages = response.get("VersionIdsToStages")
    if not isinstance(stages, dict) or not any(
        "AWSCURRENT" in _as_list(value) for value in stages.values()
    ):
        problems.append("no_current_version")
    return (arn if isinstance(arn, str) else None), problems


def _stack_template_matches(response: Any) -> bool:
    """The deployed stack's original template equals the reviewed template."""

    body = response.get("TemplateBody") if isinstance(response, dict) else None
    parsed: Any
    if isinstance(body, str):
        try:
            parsed = yaml.load(body, Loader=_StrictLoader)
        except (yaml.YAMLError, WiringError):
            return False
    elif isinstance(body, dict):
        parsed = body
    else:
        return False
    reviewed = load_yaml(_REPOSITORY_ROOT / TEMPLATE_RELATIVE_PATH)
    return _canonical(parsed) == _canonical(reviewed)


def _stack_resources_exact(response: Any, smoke_arn: str | None) -> bool:
    rows = (
        response.get("StackResourceSummaries") if isinstance(response, dict) else None
    )
    if not isinstance(rows, list) or len(rows) != len(STACK_RESOURCES):
        return False
    observed = {
        (row.get("LogicalResourceId"), row.get("ResourceType"))
        for row in rows
        if isinstance(row, dict)
        and row.get("ResourceStatus") in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
    }
    secret = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("LogicalResourceId") == "BillingObservabilitySmokeTokenSecret"
    ]
    return (
        observed == set(STACK_RESOURCES)
        and len(secret) == 1
        and smoke_arn is not None
        and secret[0].get("PhysicalResourceId") == smoke_arn
    )


def _change_set_name(stack: dict[str, Any]) -> str:
    change_set_id = str(stack.get("ChangeSetId") or "")
    return change_set_id.split(":changeSet/", 1)[-1].split("/", 1)[0]


def _plan_secret_access(
    step: _Step,
    readback: dict[str, Any],
    template_sha256: str,
    applied: dict[str, Any] | None,
) -> dict[str, str | None]:
    role = readback.get("execution_role", {})
    role_arn = (role.get("Role") or {}).get("Arn") if isinstance(role, dict) else None
    if role_arn != evidence.EXECUTION_ROLE_ARN:
        step.blocks.append("execution_role_identity_mismatch")
    else:
        step.compliant.append("execution_role_identity")
    inline = (readback.get("execution_inline_policies") or {}).get("PolicyNames")
    inline_names = set(inline) if isinstance(inline, list) else None
    if (
        inline_names is None
        or TERRAFORM_POLICY_NAME not in inline_names
        or not (inline_names <= {TERRAFORM_POLICY_NAME, WIRING_POLICY_NAME})
    ):
        step.blocks.append("execution_role_inline_policies_unexpected")
    else:
        step.compliant.append("execution_role_inline_policies_known")
    attached = (readback.get("execution_attached_policies") or {}).get(
        "AttachedPolicies"
    )
    if not (
        isinstance(attached, list)
        and [row.get("PolicyArn") for row in attached if isinstance(row, dict)]
        == [EXECUTION_MANAGED_POLICY_ARN]
    ):
        step.blocks.append("execution_role_attached_policies_unexpected")
    else:
        step.compliant.append("execution_role_attached_policies_known")
    terraform_document = _policy_document(readback.get("terraform_policy"))
    if terraform_document is None:
        step.blocks.append("terraform_policy_unreadable")
    else:
        step.info["terraform_policy_sha256"] = _sha256(terraform_document)
        if _decrypt_granted(terraform_document):
            step.compliant.append("kms_decrypt_granted_by_terraform_policy")
        else:
            step.blocks.append("kms_decrypt_not_granted_for_secret_key")

    arns: dict[str, str | None] = {}
    for env, name, source, _parameter in SECRET_REFERENCES:
        arn, problems = _secret_problems(readback["secrets"].get(env), name)
        arns[env] = arn if not problems else None
        if source == "existing":
            if problems:
                step.blocks.append(f"secret_{env}_{'_'.join(problems)}")
            else:
                step.compliant.append(f"secret_{env}_present")
        elif problems and problems != ["absent"]:
            step.blocks.append(f"secret_{env}_{'_'.join(problems)}")

    terraform_grants = _granted_secret_arns(terraform_document)
    step.info["terraform_policy_already_grants"] = sorted(
        env for env, arn in arns.items() if arn and arn in terraform_grants
    )
    smoke_present = arns[SMOKE_TOKEN_ENV] is not None
    smoke_absent = _secret_problems(
        readback["secrets"].get(SMOKE_TOKEN_ENV), SECRET_REFERENCES[2][1]
    )[1] == ["absent"]
    stack_response = readback.get("stack")
    stack_code = _error_code(stack_response)
    wiring_code = _error_code(readback.get("wiring_policy"))
    stack_rows = (
        stack_response.get("Stacks") if isinstance(stack_response, dict) else None
    )
    stack = (
        stack_rows[0]
        if isinstance(stack_rows, list)
        and len(stack_rows) == 1
        and isinstance(stack_rows[0], dict)
        else None
    )
    resolved_parameters = {
        parameter: arns[env]
        for env, _name, source, parameter in SECRET_REFERENCES
        if source == "existing" and parameter is not None
    }
    if stack_code == "ValidationError":
        if smoke_present or not smoke_absent:
            step.blocks.append("smoke_token_secret_exists_outside_stack")
        if wiring_code != "NoSuchEntity":
            step.blocks.append("wiring_policy_exists_outside_stack")
        if applied is not None:
            step.blocks.append("applied_record_but_stack_absent")
        step.changes.append(
            {
                "action": "create_cloudformation_stack",
                "stack_name": STACK_NAME,
                "template": TEMPLATE_RELATIVE_PATH,
                "template_sha256": template_sha256,
                "capabilities": list(STACK_CAPABILITIES),
                "parameters": resolved_parameters,
                "creates": [
                    {
                        "type": "AWS::SecretsManager::Secret",
                        "name": SECRET_REFERENCES[2][1],
                        "kms_key_arn": KMS_KEY_ARN,
                        "value": "generated_by_secrets_manager",
                    },
                    {
                        "type": "AWS::IAM::Policy",
                        "role_name": EXECUTION_ROLE_NAME,
                        "policy_name": WIRING_POLICY_NAME,
                        "document": expected_wiring_policy(
                            [
                                str(arns[env])
                                for env in ADDED_SECRETS
                                if env != SMOKE_TOKEN_ENV and arns[env]
                            ]
                            + ["<smoke token ARN assigned by the stack>"]
                        ),
                    },
                ],
            }
        )
        return arns
    if stack is None:
        step.blocks.append("stack_readback_unavailable")
        return arns
    status = stack.get("StackStatus")
    if status not in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
        step.blocks.append(f"stack_status:{status}")
        return arns
    observed = {
        row.get("ParameterKey"): row.get("ParameterValue")
        for row in stack.get("Parameters") or []
        if isinstance(row, dict)
    }
    if observed != resolved_parameters:
        step.blocks.append("stack_parameters_drift")
    if not smoke_present:
        step.blocks.append("stack_smoke_token_secret_missing_or_invalid")
    if wiring_code is not None:
        step.blocks.append("stack_wiring_policy_missing")
    elif all(arns.values()):
        problems = wiring_policy_problems(
            _policy_document(readback.get("wiring_policy")),
            {str(arn) for arn in arns.values()},
        )
        if problems:
            step.blocks.append("wiring_policy_drift:" + ",".join(problems))
        else:
            step.compliant.append("wiring_policy_exact")
    if _stack_resources_exact(readback.get("stack_resources"), arns[SMOKE_TOKEN_ENV]):
        step.compliant.append("stack_resources_exact")
    else:
        step.blocks.append("stack_resources_mismatch")
    if _stack_template_matches(readback.get("stack_template")):
        step.compliant.append("stack_template_matches_reviewed_template")
    else:
        step.blocks.append("stack_template_differs_from_reviewed_template")
    step.info["stack_change_set_name"] = _change_set_name(stack)
    if applied is None:
        step.info["applied_record"] = "none in manifest"
    else:
        applied_blocks = []
        if applied.get("stack_name") != stack.get("StackName"):
            applied_blocks.append("applied_record_stack_mismatch")
        if applied.get("change_set_name") != _change_set_name(stack):
            applied_blocks.append("applied_record_change_set_mismatch")
        if applied.get("smoke_token_secret_arn") != arns[SMOKE_TOKEN_ENV]:
            applied_blocks.append("applied_record_smoke_token_arn_mismatch")
        step.blocks.extend(applied_blocks)
        if not applied_blocks:
            step.compliant.append("applied_record_matches_live_state")
    if not step.blocks:
        step.compliant.append(f"stack_{status}")
    return arns


def _service_view(readback: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    response = readback.get("service")
    services = response.get("services") if isinstance(response, dict) else None
    if not (
        isinstance(response, dict)
        and response.get("failures") == []
        and isinstance(services, list)
        and len(services) == 1
        and isinstance(services[0], dict)
    ):
        return None, ["service_readback_unavailable"]
    service = services[0]
    problems: list[str] = []
    if service.get("serviceArn") != rollout.SERVICE_ARN or service.get("status") != (
        "ACTIVE"
    ):
        problems.append("service_identity_mismatch")
    desired = service.get("desiredCount")
    if not (
        type(desired) is int and desired > 0 and service.get("runningCount") == desired
    ):
        problems.append("service_not_steady")
    primary = [
        row
        for row in service.get("deployments") or []
        if isinstance(row, dict) and row.get("status") == "PRIMARY"
    ]
    if len(primary) != 1 or primary[0].get("rolloutState") != "COMPLETED":
        problems.append("service_rollout_not_complete")
    if len(service.get("deployments") or []) != 1:
        problems.append("service_has_concurrent_deployments")
    return service, problems


def _host_problems(host: str, readback: dict[str, Any], alb_dns: str) -> list[str]:
    problems: list[str] = []
    records = (readback.get("record_sets") or {}).get("ResourceRecordSets")
    aliases = [
        row
        for row in records or []
        if isinstance(row, dict)
        and row.get("Type") == "A"
        and _dns(row.get("Name")) == host
    ]
    target = (
        _dns((aliases[0].get("AliasTarget") or {}).get("DNSName")) if aliases else ""
    )
    if (
        len(aliases) != 1
        or not alb_dns
        or target not in {alb_dns, f"dualstack.{alb_dns}"}
    ):
        problems.append("dns_alias_not_on_load_balancer")
    certificate = (readback.get("certificate") or {}).get("Certificate")
    names = (
        certificate.get("SubjectAlternativeNames")
        if isinstance(certificate, dict)
        else None
    )
    parent = host.split(".", 1)[1] if "." in host else ""
    if not (
        isinstance(certificate, dict)
        and certificate.get("Status") == "ISSUED"
        and isinstance(names, list)
        and (host in names or f"*.{parent}" in names)
    ):
        problems.append("certificate_does_not_cover_host")
    return problems


def _plan_task_definition(
    step: _Step,
    readback: dict[str, Any],
    parameters: dict[str, Any],
    arns: dict[str, str | None],
    service: dict[str, Any] | None,
    service_problems: list[str],
    pins: ManifestPins,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None]:
    """Return (renderable request or None, diff, request to evaluate gates on).

    The ``rollback_target_*`` reasons below name the D3 source revision, which
    was the rollback target when they were introduced (their IDs are kept).
    """

    diff: dict[str, Any] = {}
    step.blocks.extend(service_problems)
    source_response = readback.get("source_task_definition")
    source = (
        source_response.get("taskDefinition")
        if isinstance(source_response, dict)
        else None
    )
    if not isinstance(source, dict) or source.get("taskDefinitionArn") != (
        D3_SOURCE_TASK_DEFINITION_ARN
    ):
        step.blocks.append("rollback_target_unreadable")
        return None, diff, None
    if source.get("status") != "ACTIVE":
        step.blocks.append("rollback_target_not_active")
    if (
        source.get("taskRoleArn") != evidence.TASK_ROLE_ARN
        or source.get("executionRoleArn") != evidence.EXECUTION_ROLE_ARN
    ):
        step.blocks.append("rollback_target_roles_mismatch")
    try:
        source_request = _request_from_response(source_response)
    except WiringError as exc:
        step.blocks.append(str(exc))
        return None, diff, None
    passed, reason = group5_template_verdict(
        source_request, D3_SOURCE_TASK_DEFINITION_ARN
    )
    if passed:
        step.compliant.append("rollback_target_passes_group5_template_validator")
    else:
        step.blocks.append(f"rollback_target_fails_group5_template_validator:{reason}")
    forbidden = forbidden_names_in(source_request)
    if forbidden:
        step.blocks.append("rollback_target_has_forbidden_names:" + ",".join(forbidden))
    else:
        step.compliant.append("rollback_target_has_no_forbidden_names")

    app = _source_app_container(source_response) or {}
    live_image = app.get("image")
    diff["source_task_definition_arn"] = D3_SOURCE_TASK_DEFINITION_ARN
    diff["live_app_image"] = live_image
    live_images = (readback.get("live_image") or {}).get("imageDetails")
    if isinstance(live_images, list) and len(live_images) == 1:
        diff["live_app_image_digest"] = live_images[0].get("imageDigest")
        step.compliant.append("live_app_image_resolves_in_ecr")
    else:
        step.blocks.append("live_app_image_not_resolvable_in_ecr")
    override = parameters.get(IMAGE_PARAMETER)
    if override is not None:
        rows = (readback.get("override_image") or {}).get("imageDetails")
        if not (isinstance(rows, list) and len(rows) == 1):
            step.blocks.append("app_image_override_not_found_in_ecr")
    diff["rendered_app_image"] = override or live_image
    diff["app_image_changed"] = override is not None and override != live_image

    alb_rows = (readback.get("load_balancers") or {}).get("LoadBalancers")
    alb_dns = (
        _dns(alb_rows[0].get("DNSName"))
        if isinstance(alb_rows, list) and len(alb_rows) == 1
        else ""
    )
    for name in (RETURN_URL_PARAMETER, WEBHOOK_URL_PARAMETER):
        host = urlsplit(parameters[name]).hostname or ""
        problems = _host_problems(host, readback, alb_dns)
        if problems:
            step.blocks.append(f"host_not_served:{name}:{','.join(problems)}")
        else:
            step.compliant.append(f"host_served_over_https:{name}")

    diff["app_environment_added"] = {
        name: (parameters[ref] if kind == "parameter" else ref)
        for name, (kind, ref) in sorted(ADDED_ENVIRONMENT.items())
    }
    diff["app_secrets_added"] = {
        env: arns.get(env) or "pending:secret_access_stack"
        for env in sorted(ADDED_SECRETS)
    }
    diff["unchanged_app_environment_names"] = sorted(_names(app, "environment"))
    diff["unchanged_app_secret_names"] = sorted(_names(app, "secrets"))
    diff["otel_container"] = "unchanged"

    if any(arns.get(env) is None for env in ADDED_SECRETS):
        step.pending.append("secret_access_stack")
        # Internal preview so the rendering rules and the deploy-path verdict
        # are evaluated before the stack exists. Its synthetic ARNs are never
        # printed and the preview is never returned as a renderable request.
        try:
            preview = render_task_definition(
                source_response,
                secret_arns={
                    env: arns.get(env) or _preview_secret_arn(name)
                    for env, name, _source, _parameter in SECRET_REFERENCES
                },
                parameters=parameters,
                pins=pins,
            )
        except WiringError as exc:
            step.blocks.append(str(exc))
            return None, diff, None
        return None, diff, preview
    try:
        rendered = render_task_definition(
            source_response,
            secret_arns={env: str(arns[env]) for env in ADDED_SECRETS},
            parameters=parameters,
            pins=pins,
        )
    except WiringError as exc:
        step.blocks.append(str(exc))
        return None, diff, None
    diff["rendered_request_sha256"] = _sha256(rendered)

    latest_response = readback.get("latest_task_definition")
    latest = (
        latest_response.get("taskDefinition")
        if isinstance(latest_response, dict)
        else None
    )
    latest_arn = latest.get("taskDefinitionArn") if isinstance(latest, dict) else None
    current = service.get("taskDefinition") if isinstance(service, dict) else None
    latest_matches = False
    if isinstance(latest_arn, str) and latest_arn != D3_SOURCE_TASK_DEFINITION_ARN:
        try:
            latest_matches = _comparable(_request_from_response(latest_response)) == (
                _comparable(rendered)
            )
        except WiringError:
            latest_matches = False
    if current == D3_SOURCE_TASK_DEFINITION_ARN:
        if latest_arn == D3_SOURCE_TASK_DEFINITION_ARN:
            step.changes.append(
                {
                    "action": "register_task_definition",
                    "family": TASK_FAMILY,
                    "derived_from": D3_SOURCE_TASK_DEFINITION_ARN,
                    "request_sha256": diff["rendered_request_sha256"],
                    "service_changes": "none (service_deployment is a separate gate)",
                }
            )
        elif latest_matches:
            step.compliant.append(f"registered_not_deployed:{latest_arn}")
        else:
            step.blocks.append(f"newer_revision_differs_from_rendering:{latest_arn}")
    elif isinstance(current, str) and current == latest_arn and latest_matches:
        step.compliant.append(f"registered_and_deployed:{latest_arn}")
    else:
        step.blocks.append(f"service_not_on_rollback_target:{current}")
    return rendered, diff, rendered


def _plan_protected_path_integrity(
    image_gate: _Step,
    candidate: dict[str, Any],
    source_request: dict[str, Any] | None,
) -> None:
    """Report, from the real validators, what the protected deploy path rejects.

    Nothing in the deploy path is changed: this only evaluates its rules.
    """

    image_gate.info["protected_path_image_repository"] = PRODUCER_IMAGE_REPOSITORY
    try:
        app_image = evidence._container_map(candidate)[APP_CONTAINER].get("image")
    except evidence.EvidenceError:
        app_image = None
    # asaas_producer_rollout_evidence.build_replacement_task_definition and
    # verify_asaas_producer_image_rollout.validate_deployment_snapshot accept a
    # template/serving app image in the producer repository, or a bridge image
    # in the source repository pinned by digest (never by tag).
    try:
        rollout.classify_app_image(
            app_image, target_repository=PRODUCER_IMAGE_REPOSITORY
        )
    except rollout.RolloutError:
        image_gate.blocks.append("F1_protected_path_image_repository_mismatch")
    try:
        candidate_fingerprint = rollout._task_definition_runtime_fingerprint(candidate)
        source_fingerprint = (
            rollout._task_definition_runtime_fingerprint(source_request)
            if source_request is not None
            else None
        )
        probe = copy.deepcopy(candidate)
        for row in evidence._container_map(probe)[APP_CONTAINER].get("environment", []):
            if row.get("name") in ADDED_ENVIRONMENT:
                row["value"] = row.get("value", "") + "-changed"
        values_fingerprinted = (
            rollout._task_definition_runtime_fingerprint(probe) != candidate_fingerprint
        )
    except (rollout.RolloutError, evidence.EvidenceError):
        image_gate.blocks.append("F1_runtime_fingerprint_unavailable")
        return
    image_gate.info["runtime_fingerprint_rollback_target"] = source_fingerprint
    image_gate.info["runtime_fingerprint_candidate"] = candidate_fingerprint
    # compare_rollout_readbacks requires equal before/after fingerprints, and the
    # fingerprint hashes every secret reference: one protected deploy from the
    # rollback target to image + wiring (Path B) cannot pass it.
    if source_fingerprint != candidate_fingerprint:
        image_gate.blocks.append("F1_path_b_runtime_fingerprint_changes")
    # True since F1 path A: the runtime fingerprint hashes the D3 environment
    # values too, so a changed ASAAS_WEBHOOK_URL fails the integrity check.
    image_gate.info["d3_environment_values_fingerprinted"] = values_fingerprinted
    residual = residual_5_fingerprint_kept(candidate)
    image_gate.info["protected_replacement_keeps_runtime_fingerprint"] = residual
    if residual is None:
        image_gate.blocks.append("F1_residual_5_database_url_fingerprint:not_evaluated")
    elif not residual:
        image_gate.blocks.append("F1_residual_5_database_url_fingerprint")


def residual_5_fingerprint_kept(candidate: dict[str, Any]) -> bool | None:
    """Whether the protected deploy's replacement keeps the runtime fingerprint.

    F1 residual 5: the template carries the plain ``DATABASE_URL`` ARN and the
    replacement pins it to ``<ARN>:::<VersionId>``, which the fingerprint
    normalizes differently, so ``compare_rollout_readbacks`` fails after the
    service update. Evaluated with the real ``build_replacement_task_definition``
    and fingerprint on a copy of the candidate (synthetic digest and version;
    the app image is normalized out of the fingerprint). None when the
    replacement cannot be built from the candidate at all.
    """

    probe = copy.deepcopy(candidate)
    try:
        app = evidence._container_map(probe)[APP_CONTAINER]
        app["image"] = _PROBE_BRIDGE_IMAGE
        database = evidence._secret_map(app)["DATABASE_URL"]
        template = {
            "schema_version": evidence.TASK_DEFINITION_TEMPLATE_SCHEMA,
            "source_task_definition_arn": D3_SOURCE_TASK_DEFINITION_ARN,
            "task_definition": probe,
        }
        with _synthetic_trust_pins():
            replacement = evidence.build_replacement_task_definition(
                template,
                image_uri=PRODUCER_IMAGE_REPOSITORY,
                app_digest=_PROBE_DIGEST,
                pinned_database_value_from=f"{database}:::{_PROBE_SECRET_VERSION}",
            )
        return bool(
            rollout._task_definition_runtime_fingerprint(replacement)
            == rollout._task_definition_runtime_fingerprint(candidate)
        )
    except (evidence.EvidenceError, rollout.RolloutError, KeyError):
        return None


def webhook_registration_entry_point_present() -> bool:
    """Whether this tree ships ``python -m src.ops.asaas_webhook`` (fact, not pin)."""

    return (_REPOSITORY_ROOT / WEBHOOK_REGISTRATION_ENTRY_POINT).is_file()


def _preview_secret_arn(name: str) -> str:
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT_ID}:secret:{name}-PREVEW"


def _plan_rollback_target(
    deployment: _Step, readback: dict[str, Any], pins: ManifestPins
) -> None:
    """The manifest's rollback target: where a failed deployment goes back to."""

    deployment.info["rollback_target"] = pins.rollback_task_definition_arn
    deployment.info["rollback_target_effective_after"] = pins.rollback_effective_after
    deployment.info["rollback_target_must_repin_before"] = pins.rollback_repin_before
    response = (
        readback.get("source_task_definition")
        if pins.rollback_revision == D3_SOURCE_REVISION
        else readback.get("rollback_task_definition")
    )
    definition = response.get("taskDefinition") if isinstance(response, dict) else None
    app = _source_app_container(response) or {}
    image = app.get("image")
    pinned_by_digest = pins.rollback_revision == D3_SOURCE_REVISION or (
        isinstance(image, str) and _ECR_DIGEST_REFERENCE.fullmatch(image) is not None
    )
    digest = (
        isinstance(image, str) and _ECR_DIGEST_REFERENCE.fullmatch(image) is not None
    )
    deployment.info["rollback_target_image_pinned_by_digest"] = digest
    if (
        isinstance(definition, dict)
        and definition.get("taskDefinitionArn") == pins.rollback_task_definition_arn
        and definition.get("status") == "ACTIVE"
        and pinned_by_digest
    ):
        # Revision 8 runs an immutable-repository tag; only a digest is
        # reported as digest pinned.
        deployment.compliant.append(
            "rollback_target_active_and_digest_pinned"
            if digest
            else "rollback_target_active_on_initial_tagged_revision"
        )
    else:
        deployment.blocks.append("rollback_target_revision_unusable")


def _plan_gates(
    steps: dict[str, _Step],
    readback: dict[str, Any],
    service: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    evaluated_on: str,
    source_request: dict[str, Any] | None,
    pins: ManifestPins,
) -> None:
    image_gate = steps["signed_image_build_and_attestation"]
    image_gate.info["deploy_path_verdict_evaluated_on"] = evaluated_on
    # Since F1 path A the Group 5 template validator accepts the D3 rendering;
    # a rejection now is a regression and is reported under its own name.
    if candidate is not None:
        passed, reason = group5_template_verdict(
            candidate, D3_SOURCE_TASK_DEFINITION_ARN
        )
        if passed:
            image_gate.compliant.append(
                "group5_deploy_template_validator_accepts_rendering"
            )
        else:
            image_gate.blocks.append(
                f"F1_group5_template_validator_regression:{reason}"
            )
        _plan_protected_path_integrity(image_gate, candidate, source_request)
    else:
        image_gate.blocks.append(
            "F1_group5_template_validator_regression:not_evaluated"
        )

    steps[MIGRATIONS_STEP].info.update(
        {
            "alembic_version": (
                "not readable through AWS APIs; read it back with the migrations "
                "one-off task before the window"
            ),
            "required_current_revision": MIGRATIONS_SOURCE_REVISION,
            "command": " ".join(MIGRATIONS_COMMAND),
            "target_revision": MIGRATIONS_TARGET_REVISION,
            "verification": MIGRATIONS_VERIFICATION,
            "excluded_revisions": MIGRATIONS_EXCLUDED,
        }
    )

    deployment = steps["service_deployment"]
    _plan_rollback_target(deployment, readback, pins)
    config = (service or {}).get("deploymentConfiguration") or {}
    breaker = config.get("deploymentCircuitBreaker") or {}
    if (
        breaker.get("enable") is True
        and breaker.get("rollback") is True
        and config.get("minimumHealthyPercent") == 100
        and config.get("maximumPercent") == 200
    ):
        deployment.compliant.append("circuit_breaker_with_rollback_100_200")
    else:
        deployment.blocks.append("deployment_safety_settings_changed")

    spa = steps["spa_serving"]
    spa.info["task_definition_spa_names"] = "none (the image sets SPA_DIST_DIR)"

    webhook = steps["webhook_exposure_and_registration"]
    https = _https_listener(readback.get("listeners"))
    target_groups = [
        row.get("targetGroupArn")
        for row in (service or {}).get("loadBalancers") or []
        if isinstance(row, dict)
    ]
    actions = https.get("DefaultActions") if https else None
    if (
        isinstance(actions, list)
        and len(actions) == 1
        and actions[0].get("Type") == "forward"
        and len(target_groups) == 1
        and actions[0].get("TargetGroupArn") == target_groups[0]
    ):
        webhook.compliant.append("https_listener_forwards_all_paths_to_service")
    else:
        webhook.blocks.append("https_listener_default_action_unexpected")
    rules = (readback.get("https_rules") or {}).get("Rules")
    if isinstance(rules, list) and all(
        isinstance(rule, dict) and rule.get("IsDefault") is True for rule in rules
    ):
        webhook.compliant.append("no_listener_rule_shadows_receiver_path")
    else:
        webhook.blocks.append("https_listener_rules_need_review")
    web_acl = (readback.get("web_acl") or {}).get("WebACL")
    if not isinstance(web_acl, dict):
        webhook.info["waf"] = "no web ACL associated"
    else:
        enforcing = [
            str(rule.get("Name"))
            for rule in web_acl.get("Rules") or []
            if isinstance(rule, dict)
            and "Count" not in (rule.get("OverrideAction") or rule.get("Action") or {})
        ]
        if "Allow" in (web_acl.get("DefaultAction") or {}) and not enforcing:
            webhook.compliant.append("waf_count_only_blocks_nothing")
        else:
            webhook.blocks.append("waf_rule_may_block_receiver:" + ",".join(enforcing))
    if webhook_registration_entry_point_present():
        webhook.compliant.append("webhook_registration_entry_point_present")
    else:
        webhook.blocks.append("F3_webhook_registration_entry_point:missing")
    steps["rollout_transitions"].info["state"] = "not planned until credits exist"


def build_plan(
    parameters: dict[str, Any],
    readback: dict[str, Any],
    digests: dict[str, str],
    *,
    applied: dict[str, Any] | None,
    pins: ManifestPins,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Pure comparison of a readback with the manifest; no AWS access.

    ``applied`` is the manifest's record of an already applied
    secret_access_stack (``applied_record``), checked against the live stack.
    ``pins`` carries the manifest values code paths read (``manifest_pins``):
    the rollback target, the otel digest and the tag overrides.
    """

    steps = {step_id: _Step(step_id, scope) for step_id, scope in STEPS}
    preflight: list[str] = []
    caller = readback.get("caller")
    if not isinstance(caller, dict) or caller.get("Account") != ACCOUNT_ID:
        preflight.append("aws_account_mismatch")
    arns = _plan_secret_access(
        steps["secret_access_stack"], readback, digests["template_sha256"], applied
    )
    service, service_problems = _service_view(readback)
    rendered, diff, candidate = _plan_task_definition(
        steps["task_definition_revision"],
        readback,
        parameters,
        arns,
        service,
        service_problems,
        pins,
    )
    evaluated_on = (
        "rendering"
        if rendered is not None
        else ("preview" if candidate is not None else "none")
    )
    egress = steps["financial_egress_closed"]
    source_request = None
    try:
        source_request = _request_from_response(readback.get("source_task_definition"))
    except WiringError:
        egress.blocks.append("rollback_target_unreadable")
    for label, request in (
        ("rollback_target", source_request),
        (evaluated_on, candidate),
    ):
        if request is None:
            continue
        names = {
            name
            for container in request.get("containerDefinitions") or []
            if isinstance(container, dict)
            for field in ("environment", "secrets")
            for name in _names(container, field)
        }
        if any(name.startswith("APPCONFIG_") for name in names):
            egress.blocks.append(f"appconfig_names_in_{label}")
        else:
            egress.compliant.append(f"no_appconfig_names_in_{label}")
    applications = (readback.get("appconfig_applications") or {}).get("Items")
    egress.info["financial_egress_enabled"] = "false (closed by absence)"
    egress.info["appconfig_application_count"] = (
        len(applications) if isinstance(applications, list) else "unreadable"
    )
    task_role_inline = (readback.get("task_inline_policies") or {}).get("PolicyNames")
    task_role_attached = (readback.get("task_attached_policies") or {}).get(
        "AttachedPolicies"
    )
    steps["secret_access_stack"].info["task_role_change"] = {
        "required": False,
        "why": (
            "the app receives the three secrets as ECS-injected environment "
            "(execution role); no AppConfig, Secrets Manager or other AWS call "
            "is added to the running app"
        ),
        "inline_policy_names": (
            sorted(task_role_inline)
            if isinstance(task_role_inline, list)
            else "unreadable"
        ),
        "attached_policy_count": (
            len(task_role_attached)
            if isinstance(task_role_attached, list)
            else "unreadable"
        ),
    }
    _plan_gates(steps, readback, service, candidate, evaluated_on, source_request, pins)

    ordered = [steps[step_id].as_dict() for step_id, _scope in STEPS]
    blocks = [{"step": "preflight", "reason": reason} for reason in preflight] + [
        {"step": row["id"], "reason": reason}
        for row in ordered
        if row["scope"] == "d3_apply"
        for reason in row["blocks"]
    ]
    gate_blocks = [
        {"step": row["id"], "reason": reason}
        for row in ordered
        if row["scope"] == "separate_approval"
        for reason in row["blocks"]
    ]
    d3_states = {row["state"] for row in ordered if row["scope"] == "d3_apply"}
    if blocks:
        status = "blocked"
    elif d3_states == {"compliant"}:
        status = "compliant"
    else:
        status = "ready"
    plan = {
        "schema": PLAN_SCHEMA,
        "status": status,
        **digests,
        "target": {
            "account_id": ACCOUNT_ID,
            "region": REGION,
            "cluster": CLUSTER,
            "service": SERVICE,
            "rollback_target": pins.rollback_task_definition_arn,
            "current_task_definition": (service or {}).get("taskDefinition"),
        },
        "parameters": {
            name: parameters[name] for name in (*REQUIRED_PARAMETERS, IMAGE_PARAMETER)
        },
        "task_definition_diff": diff,
        "steps": ordered,
        "would_change": [
            f"{row['id']}:{change['action']}"
            for row in ordered
            for change in row["changes"]
        ],
        "compliant": [
            f"{row['id']}:{item}" for row in ordered for item in row["compliant"]
        ],
        "blocks": blocks,
        "gate_blocks": gate_blocks,
    }
    return plan, rendered


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    _require(
        _SECRET_LIKE_OUTPUT.search(text) is None, "output_contains_secret_like_material"
    )
    print(text)


def renderable(plan: dict[str, Any], rendered: dict[str, Any] | None) -> dict[str, Any]:
    """Release a rendering only from an unblocked plan with the stack compliant."""

    reasons = sorted({str(block["reason"]) for block in plan["blocks"]})
    _require(plan["status"] != "blocked", "plan_blocked:" + ",".join(reasons))
    stack_step = next(
        row for row in plan["steps"] if row["id"] == "secret_access_stack"
    )
    _require(stack_step["state"] == "compliant", "secret_access_stack_not_compliant")
    revision = next(
        row for row in plan["steps"] if row["id"] == "task_definition_revision"
    )
    _require(
        rendered is not None,
        "rendering_unavailable:"
        + ",".join(revision["blocks"] or revision["pending"] or ["unknown"]),
    )
    assert rendered is not None
    return rendered


def _aws_from_args(
    args: argparse.Namespace,
    transport_factory: Callable[[], Transport] | None,
    digests: dict[str, str],
) -> ReadOnlyAws:
    if args.readback_file is not None:
        transport = snapshot_transport(args.readback_file)
        digests["readback_file_sha256"] = _file_sha256(args.readback_file)
        digests["readback_source"] = "snapshot"
    else:
        transport = (transport_factory or boto3_transport)()
        digests["readback_source"] = "live"
    return ReadOnlyAws(transport)


def _plan_from_args(
    args: argparse.Namespace, transport_factory: Callable[[], Transport] | None
) -> tuple[dict[str, Any], dict[str, Any] | None, list[tuple[str, str]]]:
    manifest, parameters, digests = load_inputs(args.manifest, args.params)
    assert parameters is not None
    pins = manifest_pins(manifest)
    aws = _aws_from_args(args, transport_factory, digests)
    readback = collect_readback(aws, parameters, pins=pins)
    plan, rendered = build_plan(
        parameters, readback, digests, applied=applied_record(manifest), pins=pins
    )
    plan["aws_operations"] = sorted({f"{s}.{o}" for s, o in aws.calls})
    return plan, rendered, aws.calls


MIGRATIONS_ALREADY_REGISTERED_EXIT = 3


def _render_migrations_from_args(
    args: argparse.Namespace, transport_factory: Callable[[], Transport] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """(request to register, or None; the equal revision already registered)."""

    manifest, parameters, digests = load_inputs(args.manifest, args.params)
    assert parameters is not None
    aws = _aws_from_args(args, transport_factory, digests)
    readback = collect_migrations_readback(aws, parameters)
    status, rendered = plan_migrations(
        readback, parameters, pins=manifest_pins(manifest)
    )
    _require(rendered is not None, "migrations_blocked:" + ",".join(status["blocks"]))
    assert rendered is not None
    registered = status["run_task_definition"]
    if registered is not None and not args.force_new_revision:
        # Run that revision; registering an identical one again is refused.
        return None, str(registered)
    return rendered, registered


def main(
    argv: list[str] | None = None,
    *,
    transport_factory: Callable[[], Transport] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "check-manifest",
        "plan",
        "render-taskdef",
        "check",
        "render-migrations-taskdef",
    ):
        sub = subparsers.add_parser(name)
        sub.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        sub.add_argument(
            "--params", type=Path, required=name != "check-manifest", default=None
        )
        if name != "check-manifest":
            sub.add_argument("--readback-file", type=Path, default=None)
        if name == "render-migrations-taskdef":
            sub.add_argument("--force-new-revision", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "check-manifest":
            _manifest, _parameters, digests = load_inputs(args.manifest, args.params)
            _emit(
                {
                    "schema": "asaas_production_wiring_manifest_check.v1",
                    "status": "valid",
                    **digests,
                }
            )
            return 0
        if args.command == "render-migrations-taskdef":
            request, registered = _render_migrations_from_args(args, transport_factory)
            if request is None:
                print(f"already registered: {registered}", file=sys.stderr)
                return MIGRATIONS_ALREADY_REGISTERED_EXIT
            _emit(request)
            return 0
        plan, rendered, _calls = _plan_from_args(args, transport_factory)
        if args.command == "plan":
            _emit(plan)
            return 2 if plan["status"] == "blocked" else 0
        rendered = renderable(plan, rendered)
        if args.command == "render-taskdef":
            _emit(rendered)
            return 0
        results = run_evidence_validators(rendered, D3_SOURCE_TASK_DEFINITION_ARN)
        structural = all(
            row["result"] == "pass" for row in results if row["scope"] == "structural"
        )
        deploy_path = all(
            row["result"] == "pass"
            for row in results
            if row["scope"] == "group5_deploy_path"
        )
        _emit(
            {
                "schema": CHECK_SCHEMA,
                "rendered_request_sha256": _sha256(rendered),
                "structural_status": "pass" if structural else "fail",
                "group5_deploy_path_status": "pass" if deploy_path else "blocked",
                "status": "pass" if structural and deploy_path else "blocked",
                "validators": results,
            }
        )
        return 0 if structural and deploy_path else 2
    except WiringError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
