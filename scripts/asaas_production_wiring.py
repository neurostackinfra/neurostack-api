#!/usr/bin/env python3
"""Read-only plan for the Group 9 D3/D4 ASAAS production wiring.

``check-manifest`` validates the declarative manifest, its reviewed parameters
and the CloudFormation template without AWS access. ``plan`` reads AWS state
(live, or a recorded readback file), compares it with the manifest and prints a
JSON diff: what would change, what is already compliant and what blocks.
``render-taskdef`` prints the proposed task definition request, derived from the
live D3 source revision, with secrets as ARN references only, the otel image
pinned by digest and ``ManagedBy=manual``. ``check`` runs the existing rollout
evidence validators against that request. ``render-migrations-taskdef
--window 035|036|037|042 --purpose migrate|read`` (both required, no default)
prints a new ``neurostack-staging-migrations`` revision that bakes exactly
that window's ``alembic upgrade <window>`` (``migrate``) or, for window 037
only, its SELECT-only readback program run as the migration role (``read``);
revision 3 is only read.
The windows are the table of ``scripts/asaas_migration_window.py``, which also
runs them. The service rollback target is the manifest field
``rollback_target``. Once the service runs a registered D3 revision, a new
revision is proposed only for one reviewed difference at a time: one flag
value alone (the worker flag; the credit switch and the transport of
asaas-rollout-opening), the two staged flag rows added alone at "false", the
AppConfig group added alone, or the app image digest alone (the image bump).
The financial egress step knows two stages: closed by the absence of any
APPCONFIG_* name, then the complete AppConfig group (source sdk) naming an
application that exists.

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

from scripts import asaas_migration_window as migration_window  # noqa: E402
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
# It is history, not the rollback target: revisions 9 and 10 stay derived from
# it after the rollback target moves.
D3_SOURCE_REVISION = 8
D3_SOURCE_TASK_DEFINITION_ARN = task_definition_arn(TASK_FAMILY, D3_SOURCE_REVISION)
# The bridge-image D3 revision the rollback target was first re-pinned to.
BRIDGE_REVISION = 9
# The worker-flag revision (revision 9 with BILLING_FACT_WORKER_ENABLED 'true',
# same bridge image) the rollback target was re-pinned to next: the way back
# from the image bump that started from it. It is the last SAFETY re-pin.
WORKER_FLAG_REVISION = 10
# The service rollback target lives in the manifest (rollback_target); these
# are the pins it may hold and the documented step at which each one takes
# effect. Two of them are SAFETY re-pins, each with its mandatory repin note
# while it is pending: revision 8 runs code that reads columns migration 036
# drops, so the re-pin to 9 must happen before 036, and only once revision 9
# has served and its observation period closed (until then 8 is the safe way
# back). Revision 9 carries the worker flag 'false', so a rollback to it from
# an image bump would also turn the worker off: the re-pin to 10 must happen
# before the image bump is deployed, and only once revision 10 has served,
# been verified and its observation closed.
#
# From revision 10 on the target follows the live chain, one reviewed re-pin
# at a time and with no note (asaas-rollout-opening Group 5b): each
# structural deploy goes back to THE REVISION IT REPLACES. Revision 11 (the
# E7b image bump of revision 10) is the way back from live step L2a;
# revision 12 (L2a: revision 11 plus the two staged flag rows at 'false', the
# last revision before migration 042's image) from L2b and during window 042
# (L3); revision 13 (L2b: revision 12 with the image containing 042) once
# window 042 is observed (L4). Above 13 any revision may be pinned with the
# generic effective step (a hotfix image during the canary re-pins the
# target to the serving revision first). The pin's identity is proved where
# it is used, by content (_rollback_content_failures and the window tool's
# _rollback_target), never by a digest or a number in code.
ROLLBACK_EFFECTIVE_INITIAL = "initial_d3_planning_2026_10_18"
ROLLBACK_EFFECTIVE_BRIDGE = "bridge_revision_9_serving_verified_and_observation_closed"
ROLLBACK_EFFECTIVE_WORKER_FLAG = (
    "worker_flag_revision_10_serving_verified_and_observation_closed"
)
ROLLBACK_EFFECTIVE_E7B = "e7b_revision_11_serving_verified_and_observation_closed"
ROLLBACK_EFFECTIVE_STAGED_ROWS = (
    "staged_rows_revision_12_serving_verified_and_observation_closed"
)
ROLLBACK_EFFECTIVE_CREDIT_IMAGE = (
    "credit_image_revision_13_serving_verified_and_window_042_observed"
)
# The effective step of every revision above the last named pin.
ROLLBACK_EFFECTIVE_SERVING = "revision_serving_verified_and_observation_closed"
ROLLBACK_REPIN_BEFORE = "migration_036"
ROLLBACK_REPIN_BEFORE_IMAGE_BUMP = "app_image_bump_deployment"
# revision -> (effective_after, (next revision, its effective_after, before) | None)
# The second element is a pending SAFETY re-pin (the manifest must carry it as
# its repin note); None means no note, and none is accepted.
ROLLBACK_TARGET_PINS: dict[int, tuple[str, tuple[int, str, str] | None]] = {
    D3_SOURCE_REVISION: (
        ROLLBACK_EFFECTIVE_INITIAL,
        (BRIDGE_REVISION, ROLLBACK_EFFECTIVE_BRIDGE, ROLLBACK_REPIN_BEFORE),
    ),
    BRIDGE_REVISION: (
        ROLLBACK_EFFECTIVE_BRIDGE,
        (
            WORKER_FLAG_REVISION,
            ROLLBACK_EFFECTIVE_WORKER_FLAG,
            ROLLBACK_REPIN_BEFORE_IMAGE_BUMP,
        ),
    ),
    WORKER_FLAG_REVISION: (ROLLBACK_EFFECTIVE_WORKER_FLAG, None),
    11: (ROLLBACK_EFFECTIVE_E7B, None),
    12: (ROLLBACK_EFFECTIVE_STAGED_ROWS, None),
    13: (ROLLBACK_EFFECTIVE_CREDIT_IMAGE, None),
}
ROLLBACK_LAST_NAMED_PIN = max(ROLLBACK_TARGET_PINS)
# The largest app revision number the planner reads (_APP_REVISION_ARN).
ROLLBACK_PIN_MAX = 999_999_999


def rollback_target_pin(
    revision: Any,
) -> tuple[str, tuple[int, str, str] | None] | None:
    """(effective_after, pending safety re-pin) a rollback target revision
    must carry, or None when the planner never accepts it as a target.

    The named pins (8 to 13) carry their own effective step; every revision
    above them, up to the largest app revision the planner reads, carries
    the generic one with no re-pin. Anything else (7 and below, a bool, a
    string) is never a target.
    """

    if type(revision) is not int:
        return None
    pin = ROLLBACK_TARGET_PINS.get(revision)
    if pin is not None:
        return pin
    if ROLLBACK_LAST_NAMED_PIN < revision <= ROLLBACK_PIN_MAX:
        return ROLLBACK_EFFECTIVE_SERVING, None
    return None


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
# The billing.fact.v1 outbox worker (one loop per API process). The reviewed
# parameter is rendered as the environment value itself, so it is exactly the
# string "false" or "true": a YAML boolean, another spelling or surrounding
# whitespace is refused. The repository says "true" since the reviewed PR
# E7-B1; turning the worker on or off is a reviewed PR to the parameters
# file, rendered as a new revision (docs/runbooks/asaas-deployment.md, "The
# outbox worker flag").
WORKER_FLAG_PARAMETER = "BILLING_FACT_WORKER_ENABLED"
WORKER_FLAG_VALUES = ("false", "true")
# asaas-rollout-opening Decisions 12 and 13: two more reviewed flags of the
# same form (exactly the string "false" or "true"), rendered from live step
# L2a on. Both rows enter together, both "false", in a revision of their own
# that changes nothing else, the serving image included (Settings ignores
# names it does not know, so the rows change no behaviour); later each value
# flips alone, in a revision of its own: the credit switch at L5, the
# transport at L6. Every render carries both (they are required parameters).
CREDIT_FLAG_PARAMETER = "CREDIT_HOT_PATH_ENABLED"
TRANSPORT_FLAG_PARAMETER = "BILLING_FACT_TRANSPORT_ENABLED"
STAGED_FLAG_PARAMETERS = (TRANSPORT_FLAG_PARAMETER, CREDIT_FLAG_PARAMETER)
STAGED_FLAG_INITIAL_VALUE = "false"
FLAG_PARAMETERS = (WORKER_FLAG_PARAMETER, *STAGED_FLAG_PARAMETERS)
# The key a revision that flips one flag alone carries in the plan.
FLAG_CHANGE_KEYS: Mapping[str, str] = {
    WORKER_FLAG_PARAMETER: "worker_flag",
    CREDIT_FLAG_PARAMETER: "credit_hot_path_flag",
    TRANSPORT_FLAG_PARAMETER: "transport_flag",
}
IMAGE_PARAMETER = "app_image_override"
REQUIRED_PARAMETERS = (
    RETURN_URL_PARAMETER,
    WEBHOOK_URL_PARAMETER,
    EMAIL_PARAMETER,
    WORKER_FLAG_PARAMETER,
    CREDIT_FLAG_PARAMETER,
    TRANSPORT_FLAG_PARAMETER,
)
# Decision 14: the F2 AppConfig group (live step L7), all or none and only in
# a revision of its own. Three reviewed parameters, all set or all null, and
# two names the planner writes itself, never parameters: APPCONFIG_ENABLED
# "true" and APPCONFIG_SOURCE "sdk" (the only source: the task runs exactly
# app and otel, so no Agent container exists). It is the one exception to
# the forbidden prefix APPCONFIG_, and only as this complete group in the app
# environment: APPCONFIG_ORIGIN, APPCONFIG_TOKEN and any other APPCONFIG_*
# name stay forbidden everywhere.
APPCONFIG_APPLICATION_PARAMETER = "APPCONFIG_APPLICATION"
APPCONFIG_PARAMETERS = (
    APPCONFIG_APPLICATION_PARAMETER,
    "APPCONFIG_ENVIRONMENT",
    "APPCONFIG_PROFILE",
)
APPCONFIG_SOURCE_VALUE = "sdk"
# environment name -> ("parameter", parameter name) | ("value", literal)
APPCONFIG_GROUP: Mapping[str, tuple[str, str]] = {
    "APPCONFIG_APPLICATION": ("parameter", "APPCONFIG_APPLICATION"),
    "APPCONFIG_ENABLED": ("value", "true"),
    "APPCONFIG_ENVIRONMENT": ("parameter", "APPCONFIG_ENVIRONMENT"),
    "APPCONFIG_PROFILE": ("parameter", "APPCONFIG_PROFILE"),
    "APPCONFIG_SOURCE": ("value", APPCONFIG_SOURCE_VALUE),
}
# An AppConfig application, environment or configuration profile, by name or
# ID: what the SDK source passes as the identifier (no space, no separator a
# shell or URL would read).
_APPCONFIG_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RETURN_PATH = "/billing"
WEBHOOK_PATH = "/v1/billing/webhook/asaas"
# environment name -> ("parameter", parameter name) | ("value", literal)
ADDED_ENVIRONMENT: dict[str, tuple[str, str]] = {
    "ASAAS_WEBHOOK_URL": ("parameter", WEBHOOK_URL_PARAMETER),
    "ASAAS_CHECKOUT_RETURN_URL": ("parameter", RETURN_URL_PARAMETER),
    "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": ("parameter", EMAIL_PARAMETER),
    "BILLING_FACT_WORKER_ENABLED": ("parameter", WORKER_FLAG_PARAMETER),
    "BILLING_FACT_TRANSPORT_ENABLED": ("parameter", TRANSPORT_FLAG_PARAMETER),
    "CREDIT_HOT_PATH_ENABLED": ("parameter", CREDIT_FLAG_PARAMETER),
}
ADDED_SECRETS = tuple(row[0] for row in SECRET_REFERENCES)
FORBIDDEN_NAME_PREFIXES = ("ABACATEPAY_", "APPCONFIG_")
FORBIDDEN_NAMES = frozenset({"SPA_ENABLED", "SPA_DIST_DIR"})
# Four production migration windows, each its own separately approved step:
# the window table of scripts/asaas_migration_window.py is the single source
# (command, alembic_version before and after, snapshot name, readback, image
# prerequisite). Each window bakes exactly ``alembic upgrade <window>``, never
# the head, so 036 and 037 only ever run on their own owner instruction, 036
# before 037, after a fresh SELECT showing zero legacy rows, and 042 (the
# credit schema, 038 to 042 in one window) only after the image containing
# 042 serves (asaas-rollout-opening, live steps L2 then L3).
MIGRATION_WINDOWS = migration_window.WINDOWS
STEPS: tuple[tuple[str, str], ...] = (
    ("secret_access_stack", "d3_apply"),
    ("task_definition_revision", "d3_apply"),
    ("financial_egress_closed", "d3_apply"),
    ("signed_image_build_and_attestation", "separate_approval"),
    (migration_window.WINDOW_035.manifest_step, "separate_approval"),
    ("service_deployment", "separate_approval"),
    ("spa_serving", "separate_approval"),
    ("webhook_exposure_and_registration", "separate_approval"),
    (migration_window.WINDOW_036.manifest_step, "separate_approval"),
    (migration_window.WINDOW_037.manifest_step, "separate_approval"),
    (migration_window.WINDOW_042.manifest_step, "separate_approval"),
    ("rollout_transitions", "separate_approval"),
)
# What each window leaves to a later window (printed by plan).
MIGRATIONS_EXCLUDED: Mapping[str, str] = {
    "035": (
        "036 and 037: their own windows (migrations_036, migrations_037), each "
        "on its own explicit owner instruction, 036 before 037, after a fresh "
        "SELECT showing zero legacy rows"
    ),
    "036": (
        "037: its own window (migrations_037), on its own explicit owner "
        "instruction after 036 is verified"
    ),
    "037": (
        "038-042 (the additive credit schema, neurostack-credit-system): "
        "their own window (migrations_038_042), on its own explicit owner "
        "instruction after the image containing 042 serves"
    ),
    "042": (
        "043 and later: none is written; a later revision needs its own "
        "window in the table"
    ),
}
_UNPINNED_UPGRADE = re.compile(r"upgrade\s+(?:heads?\b|[+-])", re.IGNORECASE)
_ALEMBIC_UPGRADE = re.compile(r"alembic\s+upgrade\s+([^\s`'\"),.;]+)", re.IGNORECASE)
_ALEMBIC_VERSION = re.compile(r"alembic_version\s*==\s*(\w+)")
_PINNED_ALEMBIC_REVISION = re.compile(r"^[0-9]{3}$")
# The one-off migrations family. Revision 3 bakes the unpinned upgrade, so it
# is only read (roles, the database-url-migrations secret, sizing, logs) and
# never run: render-migrations-taskdef registers a new revision whose command
# is exactly the chosen window's upgrade.
MIGRATIONS_FAMILY = migration_window.MIGRATIONS_FAMILY
MIGRATIONS_TASK_SOURCE_REVISION = migration_window.MIGRATIONS_SOURCE_REVISION
MIGRATIONS_TASK_SOURCE_ARN = task_definition_arn(
    MIGRATIONS_FAMILY, MIGRATIONS_TASK_SOURCE_REVISION
)
MIGRATIONS_DATABASE_SECRET_NAME = migration_window.MIGRATIONS_DATABASE_SECRET
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


class _SpelledInt(int):
    """An integer the YAML source spells other than plain decimal (YAML 1.1
    reads ``014`` as 12, ``1:10`` as 70 and ``0xE``, ``1_4``, ``+14``,
    ``0b1110`` or ``!!int '14'`` as 14). It is an int everywhere else; it
    is never a rollback target revision (``rollback_target_pin`` requires
    ``type(...) is int``), so such a ``revision:`` fails closed."""


_PLAIN_DECIMAL = re.compile(r"0|-?[1-9][0-9]*")


def _construct_int(loader: _StrictLoader, node: yaml.ScalarNode) -> int:
    value = loader.construct_yaml_int(node)
    if node.style is None and _PLAIN_DECIMAL.fullmatch(node.value) is not None:
        return int(value)
    return _SpelledInt(value)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)
_StrictLoader.add_constructor("tag:yaml.org,2002:int", _construct_int)
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
            **{
                name: {"validator": "exact_false_or_true", "path": None}
                for name in FLAG_PARAMETERS
            },
        },
        "optional": {
            IMAGE_PARAMETER: {"validator": "ecr_image_digest_reference", "path": None},
            **{
                name: {"validator": "appconfig_identifier_all_or_none", "path": None}
                for name in APPCONFIG_PARAMETERS
            },
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
        "app_environment_appconfig_group": {
            name: {kind: reference}
            for name, (kind, reference) in APPCONFIG_GROUP.items()
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
        "command": "per_window_exact_upgrade",
        "windows": list(MIGRATION_WINDOWS),
        "read_revisions": [
            name
            for name, window in MIGRATION_WINDOWS.items()
            if migration_window.needs_read_revision(window)
        ],
        "tag_overrides": dict(TAG_OVERRIDES),
    }


# What the ``why`` of a pending safety re-pin must name, by the revision it
# re-pins TO: the reason the pin it leaves is unsafe from its ``before`` step
# on. Every re-pin of ROLLBACK_TARGET_PINS has exactly one entry (checked on
# import below).
ROLLBACK_REPIN_WHY_NAMES: Mapping[int, str] = {
    BRIDGE_REVISION: "036",
    WORKER_FLAG_REVISION: WORKER_FLAG_PARAMETER,
}


def _assert_rollback_table() -> None:
    """The pin table is one chain (8 to 13, then open), its safety re-pins
    lead to the next pin with that pin's own effective step and come before
    every pin without a note, each re-pin has exactly one ``why`` name, and
    no two pins share an effective step. Checked on import, so a widened or
    inconsistent table never loads."""

    revisions = sorted(ROLLBACK_TARGET_PINS)
    _require(
        revisions == list(range(D3_SOURCE_REVISION, ROLLBACK_LAST_NAMED_PIN + 1))
        and ROLLBACK_LAST_NAMED_PIN < ROLLBACK_PIN_MAX,
        "rollback_table_not_one_chain",
    )
    steps = [effective for effective, _repin in ROLLBACK_TARGET_PINS.values()]
    steps.append(ROLLBACK_EFFECTIVE_SERVING)
    _require(len(set(steps)) == len(steps), "rollback_table_effective_after_reused")
    repinned_to: list[int] = []
    noted = True
    for revision in revisions:
        _effective, repin = ROLLBACK_TARGET_PINS[revision]
        if repin is None:
            noted = False
            continue
        _require(noted, "rollback_table_repin_after_an_open_pin")
        target, effective, before = repin
        _require(
            target == revision + 1
            and ROLLBACK_TARGET_PINS[target][0] == effective
            and isinstance(before, str)
            and bool(before),
            "rollback_table_repin_invalid",
        )
        repinned_to.append(target)
    _require(
        sorted(ROLLBACK_REPIN_WHY_NAMES) == repinned_to
        and all(
            isinstance(name, str) and bool(name)
            for name in ROLLBACK_REPIN_WHY_NAMES.values()
        ),
        "rollback_table_why_names_mismatch",
    )


_assert_rollback_table()


def _validate_rollback_target(section: Any) -> None:
    """Only the accepted revisions, each with its documented effective step;
    the repin note exactly on the pins whose safety re-pin is pending."""

    revision = section.get("revision") if isinstance(section, dict) else None
    pin = rollback_target_pin(revision)
    _require(pin is not None, "manifest_rollback_target_invalid")
    assert isinstance(section, dict) and pin is not None
    effective_after, repin = pin
    expected_keys = {"revision", "effective_after"} | ({"repin"} if repin else set())
    _require(
        set(section) == expected_keys and section["effective_after"] == effective_after,
        "manifest_rollback_target_invalid",
    )
    if repin is not None:
        # Unreachable while the import check holds; never a bare KeyError.
        why = ROLLBACK_REPIN_WHY_NAMES.get(repin[0])
        _require(why is not None, "rollback_repin_why_missing")
        assert why is not None
        note = section["repin"]
        _require(
            isinstance(note, dict)
            and set(note) == {"revision", "effective_after", "before", "why"}
            and type(note["revision"]) is int
            and (note["revision"], note["effective_after"], note["before"]) == repin
            and isinstance(note["why"], str)
            and why in note["why"],
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
    _validate_migration_windows(document, serialized)
    return document


def _validate_migration_windows(document: dict[str, Any], serialized: str) -> None:
    """Each window step names exactly its own upgrade; nothing else names one.

    The step of window W says ``alembic upgrade W`` once (in its change, in
    backticks, with ``--window W`` and its snapshot name pattern), reads back
    ``alembic_version == <before>`` first and ``== W`` after; no other text of
    the manifest names an upgrade, and nothing names the head or a relative
    target.
    """

    window_steps = {window.manifest_step for window in MIGRATION_WINDOWS.values()}
    remainder = dict(document)
    remainder["steps"] = [
        row for row in document["steps"] if row["id"] not in window_steps
    ]
    _require(
        _UNPINNED_UPGRADE.search(serialized) is None
        and _ALEMBIC_UPGRADE.findall(_canonical(remainder)) == [],
        "manifest_migrations_window_upgrade_outside_its_step",
    )
    steps = {row["id"]: row for row in document["steps"]}
    for window in MIGRATION_WINDOWS.values():
        step = steps[window.manifest_step]
        text = _canonical(step)
        purposes = ["migrate"] + (
            ["read"] if migration_window.needs_read_revision(window) else []
        )
        _require(
            _ALEMBIC_UPGRADE.findall(text) == [window.after]
            and f"`{window.command_text}`" in step["change"]
            and all(
                f"--window {window.name} --purpose {purpose}" in step["change"]
                for purpose in purposes
            )
            and ("--purpose read" in step["change"]) == ("read" in purposes)
            and window.snapshot_pattern in text,
            f"manifest_migrations_window_not_pinned:{window.name}",
        )
        preconditions = "\n".join(step["preconditions"])
        verification = "\n".join(step["verification"])
        _require(
            _ALEMBIC_VERSION.findall(preconditions) == [window.before]
            and window.verification in verification
            and _ALEMBIC_VERSION.findall(verification) == [window.after],
            f"manifest_migrations_window_readback_not_pinned:{window.name}",
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
    allowed = set(REQUIRED_PARAMETERS) | {IMAGE_PARAMETER} | set(APPCONFIG_PARAMETERS)
    unknown = sorted(set(values) - allowed)
    _require(not unknown, f"parameter_unknown:{','.join(unknown)}")
    # A YAML boolean (true, yes, on, True...) or a number is a present but
    # invalid flag, never a missing one: the reviewed value is the quoted
    # environment string itself.
    for name in FLAG_PARAMETERS:
        flag = values.get(name)
        _require(flag is None or isinstance(flag, str), f"parameter_invalid:{name}")
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
    for name in FLAG_PARAMETERS:
        _require(output[name] in WORKER_FLAG_VALUES, f"parameter_invalid:{name}")
    # The AppConfig group: all three null (no group rendered) or all three
    # reviewed identifiers (the complete group rendered); never a part.
    appconfig = [values.get(name) for name in APPCONFIG_PARAMETERS]
    _require(
        all(value is None for value in appconfig)
        or all(value is not None for value in appconfig),
        "parameter_invalid:appconfig_group_partial",
    )
    for name, value in zip(APPCONFIG_PARAMETERS, appconfig):
        _require(
            value is None
            or (
                isinstance(value, str)
                and _APPCONFIG_IDENTIFIER.fullmatch(value) is not None
            ),
            f"parameter_invalid:{name}",
        )
        output[name] = value
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
    # The revision the service runs, read only when no other read holds it
    # (source, rollback target, latest): a newer revision registered from the
    # rendering is reported registered but not deployed only when the serving
    # revision differs from it in the worker flag alone (worker_flag_change)
    # or in the app image digest alone (app_image_change).
    serving = _serving_revision(readback["service"])
    readback["serving_task_definition"] = (
        aws.call(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": f"{TASK_FAMILY}:{serving}", "include": ["TAGS"]},
        )
        if serving is not None
        and serving not in {D3_SOURCE_REVISION, pins.rollback_revision}
        and task_definition_arn(TASK_FAMILY, serving)
        != _task_definition_arn_of(readback["latest_task_definition"])
        else None
    )
    return readback


_APP_REVISION_ARN = re.compile(
    rf"^arn:aws:ecs:{REGION}:{ACCOUNT_ID}:task-definition/"
    rf"{re.escape(TASK_FAMILY)}:(?P<revision>[1-9][0-9]{{0,8}})$"
)


def _serving_revision(response: Any) -> int | None:
    """The app revision number the service runs, or None when unreadable."""

    services = response.get("services") if isinstance(response, dict) else None
    if not (isinstance(services, list) and len(services) == 1):
        return None
    current = (
        services[0].get("taskDefinition") if isinstance(services[0], dict) else None
    )
    match = _APP_REVISION_ARN.fullmatch(current) if isinstance(current, str) else None
    return int(match.group("revision")) if match else None


def _task_definition_arn_of(response: Any) -> Any:
    definition = response.get("taskDefinition") if isinstance(response, dict) else None
    return definition.get("taskDefinitionArn") if isinstance(definition, dict) else None


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


def appconfig_group_values(request: Any) -> dict[str, str] | None:
    """The AppConfig group's values when the app environment carries exactly
    the complete group: each of its five names once, ``APPCONFIG_ENABLED``
    "true", ``APPCONFIG_SOURCE`` "sdk", the three identifiers reviewed. None
    otherwise (absent, partial, another value, a malformed request)."""

    containers = (
        request.get("containerDefinitions") if isinstance(request, dict) else None
    )
    apps = [
        container
        for container in (containers if isinstance(containers, list) else [])
        if isinstance(container, dict) and container.get("name") == APP_CONTAINER
    ]
    if len(apps) != 1 or not isinstance(apps[0].get("environment", []), list):
        return None
    rows = [
        row
        for row in apps[0].get("environment", [])
        if isinstance(row, dict) and row.get("name") in APPCONFIG_GROUP
    ]
    values = {str(row["name"]): row.get("value") for row in rows}
    if len(rows) != len(APPCONFIG_GROUP) or set(values) != set(APPCONFIG_GROUP):
        return None
    for name, (kind, reference) in APPCONFIG_GROUP.items():
        value = values[name]
        if kind == "value" and value != reference:
            return None
        if kind == "parameter" and not (
            isinstance(value, str) and _APPCONFIG_IDENTIFIER.fullmatch(value)
        ):
            return None
    return {name: str(value) for name, value in sorted(values.items())}


def forbidden_names_in(request: dict[str, Any]) -> list[str]:
    """Every forbidden env/secret name in any container (empty when clean).

    The AppConfig group is exempt from the forbidden prefix only as the
    complete, valid group (``appconfig_group_values``) in the app
    environment: a partial or altered group, a group name anywhere else, and
    every other ``APPCONFIG_*`` name are reported.
    """

    containers = request.get("containerDefinitions")
    _require(isinstance(containers, list), "task_definition_containers_invalid")
    assert isinstance(containers, list)
    exempt = set(APPCONFIG_GROUP) if appconfig_group_values(request) else set()
    found: list[str] = []
    for container in containers:
        _require(isinstance(container, dict), "task_definition_container_invalid")
        for field in ("environment", "secrets"):
            for name in _names(container, field):
                if (
                    container.get("name") == APP_CONTAINER
                    and field == "environment"
                    and name in exempt
                ):
                    continue
                if name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_NAME_PREFIXES):
                    found.append(name)
    return sorted(found)


def appconfig_names_in(request: dict[str, Any]) -> list[str]:
    """Every ``APPCONFIG_*`` env/secret name in any container."""

    return sorted(
        name
        for container in request.get("containerDefinitions") or []
        if isinstance(container, dict)
        for field in ("environment", "secrets")
        for name in _names(container, field)
        if name.startswith("APPCONFIG_")
    )


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
        not (set(ADDED_ENVIRONMENT) | set(ADDED_SECRETS) | set(APPCONFIG_GROUP))
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
        _require(
            reference not in FLAG_PARAMETERS or value in WORKER_FLAG_VALUES,
            f"parameter_invalid:{reference}",
        )
        environment.append({"name": name, "value": value})
    # The AppConfig group, only when its three parameters are set (all or
    # none: validate_parameters refuses a part), and then complete.
    appconfig = [parameters.get(name) for name in APPCONFIG_PARAMETERS]
    _require(
        all(value is None for value in appconfig)
        or all(isinstance(value, str) for value in appconfig),
        "parameter_invalid:appconfig_group_partial",
    )
    if appconfig[0] is not None:
        for name in sorted(APPCONFIG_GROUP):
            kind, reference = APPCONFIG_GROUP[name]
            value = parameters[reference] if kind == "parameter" else reference
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
    _require(
        (appconfig[0] is None) == (appconfig_group_values(request) is None),
        "parameter_invalid:appconfig_group",
    )
    return request


# What a migrations revision is for: ``migrate`` bakes the window's exact
# upgrade; ``read`` (window 037 only) bakes the window's SELECT-only readback
# program, run as the migration role, which never invokes the migration tool
# (it reads the alembic_version table; as a backstop the rendered command,
# entry point and environment never mention the tool).
MIGRATIONS_PURPOSES = ("migrate", "read")


def _read_spec(window: migration_window.MigrationWindow) -> Any:
    """The window's read that runs as the migration role."""

    for spec in (window.pre_read, window.post_read):
        if spec.role == migration_window.READ_ROLE_MIGRATIONS:
            return spec
    raise WiringError(f"migrations_read_revision_not_needed:{window.name}")


def migrations_command(
    window: migration_window.MigrationWindow, purpose: str
) -> tuple[str, ...]:
    """The command a migrations revision bakes for this window and purpose."""

    _require(purpose in MIGRATIONS_PURPOSES, "migrations_purpose_unknown")
    if purpose == "migrate":
        return tuple(window.command)
    return (
        "python",
        "-c",
        migration_window.read_program(window, _read_spec(window)),
    )


def _migrations_command_pinned(
    window: Any, purpose: Any, command: tuple[str, ...]
) -> bool:
    """The window is a table entry and the command is exactly its baked one."""

    name = getattr(window, "name", None)
    if not (
        isinstance(name, str)
        and MIGRATION_WINDOWS.get(name) == window
        and _PINNED_ALEMBIC_REVISION.fullmatch(window.after) is not None
        and tuple(window.command) == ("alembic", "upgrade", window.after)
    ):
        return False
    if purpose == "migrate":
        return tuple(command) == tuple(window.command)
    if purpose != "read":
        return False
    # migrations_command refuses a window without a migration-role read.
    normalized = migration_window.normalized_read_command(list(command))
    return (
        normalized is not None
        and normalized
        == migration_window.normalized_read_command(
            list(migrations_command(window, "read"))
        )
        and migration_window._MIGRATION_TOOL.search(json.dumps(list(command))) is None
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
    window: migration_window.MigrationWindow,
    purpose: str,
    command: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """A new migrations revision: revision 3 with the image and command replaced.

    The command is baked into the revision (no override at run time). For
    ``purpose="migrate"`` it must be exactly the window's ``alembic upgrade
    <window>``; for ``purpose="read"`` (window 037 only) exactly the window's
    SELECT-only readback program, with no mention of the migration tool in the
    command, entry point or environment. Anything else is refused. Roles, the
    ``database-url-migrations`` secret, sizing and logs come from revision 3,
    which is only read and never run.
    """

    _require(purpose in MIGRATIONS_PURPOSES, "migrations_purpose_unknown")
    # For "read", migrations_command refuses a window without a migration-role
    # read (migrations_read_revision_not_needed:<window>).
    baked = (
        migrations_command(window, purpose)
        if command is None and isinstance(window, migration_window.MigrationWindow)
        else (command or ())
    )
    _require(
        _migrations_command_pinned(window, purpose, baked),
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
    container["command"] = list(baked)
    if purpose == "read":
        _require(
            all(
                migration_window._MIGRATION_TOOL.search(
                    json.dumps(container.get(field))
                )
                is None
                for field in ("command", "entryPoint", "environment")
            ),
            "migrations_read_revision_names_the_migration_tool",
        )
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


def _window_of_revision(
    latest_response: Any, source_response: Any, pins: ManifestPins
) -> tuple[migration_window.MigrationWindow, str] | None:
    """The (window, purpose) a registered revision exactly renders, if any."""

    definition = (
        latest_response.get("taskDefinition")
        if isinstance(latest_response, dict)
        else None
    )
    rows = (
        definition.get("containerDefinitions") if isinstance(definition, dict) else None
    )
    if not (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)):
        return None
    command, image = rows[0].get("command"), rows[0].get("image")
    for window in MIGRATION_WINDOWS.values():
        for purpose in MIGRATIONS_PURPOSES:
            if purpose == "read" and not migration_window.needs_read_revision(window):
                continue
            expected = list(migrations_command(window, purpose))
            if not isinstance(image, str) or (
                command != expected
                and (
                    migration_window.normalized_read_command(command) is None
                    or migration_window.normalized_read_command(command)
                    != migration_window.normalized_read_command(expected)
                )
            ):
                continue
            try:
                rendered = render_migrations_task_definition(
                    source_response,
                    image=image,
                    pins=pins,
                    window=window,
                    purpose=purpose,
                )
                same = _comparable(
                    _request_from_response(_without_fault_injection(latest_response))
                ) == _comparable(rendered)
            except WiringError:
                return None
            return (window, purpose) if same else None
    return None


def plan_migrations(
    readback: dict[str, Any],
    parameters: dict[str, Any],
    *,
    pins: ManifestPins,
    window: migration_window.MigrationWindow,
    purpose: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Status of the window's migrations revision plus the request, or None."""

    image = parameters[IMAGE_PARAMETER]
    status: dict[str, Any] = {
        "family": MIGRATIONS_FAMILY,
        "read_only_source": MIGRATIONS_TASK_SOURCE_ARN,
        "window": getattr(window, "name", None),
        "purpose": purpose,
        "command": (
            " ".join(getattr(window, "command", ()))
            if purpose == "migrate"
            else "SELECT-only readback program (python -c)"
        ),
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
            readback.get("source"),
            image=image,
            pins=pins,
            window=window,
            purpose=purpose,
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
        order = list(MIGRATION_WINDOWS)
        previous = (
            None
            if same
            else _window_of_revision(latest_response, readback.get("source"), pins)
        )
        if same and isinstance(latest_arn, str):
            status["state"] = f"registered:{latest_arn}"
            status["run_task_definition"] = latest_arn
        elif previous is not None and (
            order.index(previous[0].name) < order.index(window.name)
            or (previous[0].name == window.name and previous[1] != purpose)
        ):
            # The newest revision is an exact rendering of an earlier window,
            # or of this window's other purpose: this one gets its own new
            # revision (one migrates, the other only reads).
            status["state"] = "would_register"
            status["newest_revision_window"] = previous[0].name
            status["newest_revision_purpose"] = previous[1]
        elif previous is not None and previous[0].name != window.name:
            status["blocks"].append(
                f"migrations_newest_revision_is_a_later_window:{latest_arn}"
            )
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
            # A baked read program compares by its SQL digest, not by the
            # gzip bytes of whichever zlib build rendered it.
            read = migration_window.normalized_read_command(container.get("command"))
            if read is not None:
                container["command"] = read
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

    # The AppConfig group too, from L7 on (its three parameters set; all or
    # none, validate_parameters refuses a part).
    added_environment = dict(ADDED_ENVIRONMENT)
    if all(isinstance(parameters.get(name), str) for name in APPCONFIG_PARAMETERS):
        added_environment.update(APPCONFIG_GROUP)
    diff["app_environment_added"] = {
        name: (parameters[ref] if kind == "parameter" else ref)
        for name, (kind, ref) in sorted(added_environment.items())
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
    # Once the service runs a registered D3 revision, a new revision is
    # proposed only for ONE reviewed difference from it (reviewed_change):
    # one flag value flipped alone (the worker; the credit switch, L5; the
    # transport, L6), the two staged flag rows added alone at "false" (L2a),
    # the AppConfig group added alone (L7), or the app image digest moved
    # alone (the image bump; L2b). Anything else matches no kind and blocks:
    # an image and a flag never share a revision (owner decision 2026-10-22,
    # Decision 13), nor do two kinds.
    serving_registered = isinstance(current, str) and current not in {
        latest_arn,
        D3_SOURCE_TASK_DEFINITION_ARN,
    }
    change = (
        reviewed_change(latest_response, rendered)
        if isinstance(current, str)
        and current == latest_arn
        and current != D3_SOURCE_TASK_DEFINITION_ARN
        and not latest_matches
        else None
    )
    serving_change = (
        reviewed_change(_serving_response(readback, str(current), pins), rendered)
        if serving_registered and latest_matches
        else None
    )
    # Stuck, never green: the service runs the rendering (or differs from it
    # in one reviewed kind) while the newest registered revision differs from
    # the rendering in that same kind alone, e.g. a revision registered and
    # then abandoned or rolled back. Reported under its own name; what to do
    # with that newest revision is an owner decision (runbook).
    newest_change = None
    if serving_registered and not latest_matches:
        candidate = reviewed_change(latest_response, rendered)
        if candidate is not None and _same_but_for(
            candidate[0], _serving_response(readback, str(current), pins), rendered
        ):
            newest_change = candidate
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
    elif change is not None:
        step.changes.append(
            {
                "action": "register_task_definition",
                "family": TASK_FAMILY,
                "derived_from": D3_SOURCE_TASK_DEFINITION_ARN,
                "replaces": current,
                change[0]: _change_entry(*change),
                "request_sha256": diff["rendered_request_sha256"],
                "service_changes": "none (service_deployment is a separate gate)",
            }
        )
    elif serving_change is not None:
        step.compliant.append(f"registered_not_deployed:{latest_arn}")
        step.info[_SERVING_INFO_KEYS[serving_change[0]]] = {
            "serving": current,
            **_serving_entry(*serving_change),
        }
    elif newest_change is not None:
        kind = newest_change[0]
        step.blocks.append(
            f"newest_revision_differs_only_in_{_NEWEST_SUFFIXES[kind]}:{latest_arn}"
        )
        step.info[f"newest_revision_{_NEWEST_SUFFIXES[kind]}"] = {
            "newest": latest_arn,
            **_newest_entry(*newest_change),
            "serving": current,
        }
    elif current == pins.rollback_task_definition_arn:
        # The service runs its own rollback target and the plan cannot move
        # from it (e.g. rolled back by hand to R after L6: the rendering is
        # R plus two flips). Not "off the rollback target": a dead end of
        # more than one kind of change; the way out is the previous revision
        # or the flag flip back (runbook).
        step.blocks.append(f"service_on_rollback_target_plan_stuck:{current}")
    else:
        step.blocks.append(f"service_not_on_rollback_target:{current}")
    return rendered, diff, rendered


def _same_but_for_worker_flag(response: Any, rendered: dict[str, Any]) -> bool:
    """Whether a registered revision equals the rendering, flag value aside."""

    return _same_but_for(FLAG_CHANGE_KEYS[WORKER_FLAG_PARAMETER], response, rendered)


def _same_but_for(kind: str, response: Any, rendered: dict[str, Any]) -> bool:
    """Whether a registered revision equals the rendering, or differs from it
    in exactly one reviewed difference of this kind."""

    try:
        same = _comparable(_request_from_response(response)) == _comparable(rendered)
    except WiringError:
        return False
    if same:
        return True
    change = reviewed_change(response, rendered)
    return change is not None and change[0] == kind


def _serving_response(
    readback: dict[str, Any], current: str, pins: ManifestPins
) -> Any:
    """The DescribeTaskDefinition readback of the revision the service runs."""

    if current == pins.rollback_task_definition_arn:
        return readback.get("rollback_task_definition")
    return readback.get("serving_task_definition")


def _serving_request_for_egress(
    readback: dict[str, Any], service: dict[str, Any] | None, pins: ManifestPins
) -> tuple[dict[str, Any] | None, str]:
    """(request, AppConfig stage) of the revision the service runs.

    Whichever read holds it: the D3 source, the newest registered revision,
    the rollback target or the serving read (collect_readback reads the
    serving revision only when no other read holds it). The stage is
    ``closed_by_absence`` (no APPCONFIG_* name), ``appconfig_group_sdk``
    (exactly the complete group), ``appconfig_names_not_the_group`` (any
    other APPCONFIG_* name) or ``unreadable``.
    """

    current = service.get("taskDefinition") if isinstance(service, dict) else None
    if not isinstance(current, str):
        return None, "unreadable"
    if current == D3_SOURCE_TASK_DEFINITION_ARN:
        response = readback.get("source_task_definition")
    elif current == _task_definition_arn_of(readback.get("latest_task_definition")):
        response = readback.get("latest_task_definition")
    else:
        response = _serving_response(readback, current, pins)
    if _task_definition_arn_of(response) != current:
        return None, "unreadable"
    try:
        request = _request_from_response(response)
        names = appconfig_names_in(request)
    except WiringError:
        return None, "unreadable"
    if not names:
        return request, "closed_by_absence"
    if appconfig_group_values(request) is not None and names == sorted(APPCONFIG_GROUP):
        return request, "appconfig_group_sdk"
    return request, "appconfig_names_not_the_group"


def worker_flag_change(
    registered_response: Any, rendered: dict[str, Any]
) -> tuple[str, str] | None:
    """(registered value, rendered value) of a worker-flag-only difference."""

    return flag_change(registered_response, rendered, WORKER_FLAG_PARAMETER)


def flag_change(
    registered_response: Any, rendered: dict[str, Any], name: str
) -> tuple[str, str] | None:
    """(registered value, rendered value) of a difference in one flag alone.

    A registered revision differs from the rendering in the flag ``name``
    (one of ``FLAG_PARAMETERS``) alone when both carry it exactly once with a
    reviewed value ("false" or "true"), the two values differ, and with the
    rendered value put in its place the revision compares equal to the
    rendering (every other field, name, value, the other flags, secret
    reference, image and tag). Anything else is None.
    """

    _require(name in FLAG_PARAMETERS, "flag_unknown")
    try:
        registered = _request_from_response(registered_response)
        registered_app = evidence._container_map(registered)[APP_CONTAINER]
        rendered_env = evidence._env_map(
            evidence._container_map(rendered)[APP_CONTAINER]
        )
        evidence._env_map(registered_app)
    except (WiringError, evidence.EvidenceError):
        return None
    rows = [
        row for row in registered_app.get("environment", []) if row.get("name") == name
    ]
    to_value = rendered_env.get(name)
    if len(rows) != 1:
        return None
    from_value = rows[0]["value"]
    if not (
        from_value in WORKER_FLAG_VALUES
        and to_value in WORKER_FLAG_VALUES
        and from_value != to_value
    ):
        return None
    rows[0]["value"] = to_value
    if _comparable(registered) != _comparable(rendered):
        return None
    return str(from_value), str(to_value)


def flag_rows_added(
    registered_response: Any, rendered: dict[str, Any]
) -> tuple[str, ...] | None:
    """The staged flag names of a difference in their rows alone (L2a).

    Decision 13: a registered revision that carries neither staged flag row,
    against a rendering that carries both, each exactly "false", and that
    with both rows added compares equal to the rendering (the same image and
    every other field). One row without the other, a "true", or any other
    difference is None.
    """

    try:
        registered = _request_from_response(registered_response)
        registered_app = evidence._container_map(registered)[APP_CONTAINER]
        rendered_env = evidence._env_map(
            evidence._container_map(rendered)[APP_CONTAINER]
        )
        evidence._env_map(registered_app)
    except (WiringError, evidence.EvidenceError):
        return None
    environment = list(registered_app.get("environment", []))
    if any(row.get("name") in STAGED_FLAG_PARAMETERS for row in environment):
        return None
    if any(
        rendered_env.get(name) != STAGED_FLAG_INITIAL_VALUE
        for name in STAGED_FLAG_PARAMETERS
    ):
        return None
    registered_app["environment"] = environment + [
        {"name": name, "value": STAGED_FLAG_INITIAL_VALUE}
        for name in STAGED_FLAG_PARAMETERS
    ]
    if _comparable(registered) != _comparable(rendered):
        return None
    return STAGED_FLAG_PARAMETERS


def appconfig_group_added(
    registered_response: Any, rendered: dict[str, Any]
) -> dict[str, str] | None:
    """The group's values of a difference in the AppConfig group alone (L7).

    A registered revision without any ``APPCONFIG_*`` name, against a
    rendering whose app environment carries the complete group
    (``appconfig_group_values``), that with the group's five rows added
    compares equal to the rendering. Anything else is None.
    """

    group = appconfig_group_values(rendered)
    if group is None:
        return None
    try:
        registered = _request_from_response(registered_response)
        registered_app = evidence._container_map(registered)[APP_CONTAINER]
        evidence._env_map(registered_app)
        present = appconfig_names_in(registered)
    except (WiringError, evidence.EvidenceError):
        return None
    if present:
        return None
    registered_app["environment"] = list(registered_app.get("environment", [])) + [
        {"name": name, "value": value} for name, value in group.items()
    ]
    if _comparable(registered) != _comparable(rendered):
        return None
    return group


def reviewed_change(
    registered_response: Any, rendered: dict[str, Any]
) -> tuple[str, Any] | None:
    """(kind, detail) of the one reviewed difference between a registered
    revision and the rendering, or None (equal, or any other difference).

    Each kind requires every other field equal, so at most one matches.
    """

    for name in FLAG_PARAMETERS:
        values = flag_change(registered_response, rendered, name)
        if values is not None:
            return FLAG_CHANGE_KEYS[name], (name, *values)
    rows = flag_rows_added(registered_response, rendered)
    if rows is not None:
        return "flag_rows_added", rows
    group = appconfig_group_added(registered_response, rendered)
    if group is not None:
        return "appconfig_group_added", group
    image = app_image_change(registered_response, rendered)
    if image is not None:
        return "app_image", image
    return None


# How each kind of reviewed difference is reported: the plan's change, the
# info key of a registered-not-deployed revision, the name of the stuck
# state (newest_revision_differs_only_in_<suffix>).
_SERVING_INFO_KEYS: Mapping[str, str] = {
    **{key: f"registered_revision_changes_{key}" for key in FLAG_CHANGE_KEYS.values()},
    "flag_rows_added": "registered_revision_adds_flag_rows",
    "appconfig_group_added": "registered_revision_adds_appconfig_group",
    "app_image": "registered_revision_changes_app_image",
}
_NEWEST_SUFFIXES: Mapping[str, str] = {
    **{key: key for key in FLAG_CHANGE_KEYS.values()},
    "flag_rows_added": "flag_rows",
    "appconfig_group_added": "appconfig_group",
    "app_image": "app_image",
}


def _appconfig_entry(group: dict[str, str]) -> dict[str, Any]:
    return {
        "names": sorted(group),
        "application": group["APPCONFIG_APPLICATION"],
        "environment": group["APPCONFIG_ENVIRONMENT"],
        "profile": group["APPCONFIG_PROFILE"],
        "source": group["APPCONFIG_SOURCE"],
    }


def _change_entry(kind: str, detail: Any) -> dict[str, Any]:
    if kind == "app_image":
        return _image_change_info(detail)
    if kind == "flag_rows_added":
        return {"names": list(detail), "value": STAGED_FLAG_INITIAL_VALUE}
    if kind == "appconfig_group_added":
        return _appconfig_entry(detail)
    name, from_value, to_value = detail
    return {"name": name, "from": from_value, "to": to_value}


def _serving_entry(kind: str, detail: Any) -> dict[str, Any]:
    entry = _change_entry(kind, detail)
    if kind in FLAG_CHANGE_KEYS.values():
        entry.pop("name")
    return entry


def _newest_entry(kind: str, detail: Any) -> dict[str, Any]:
    if kind == "app_image":
        image = _image_change_info(detail)
        return {
            "repository": image["repository"],
            "newest_digest": image["from"],
            "rendered_digest": image["to"],
        }
    if kind in FLAG_CHANGE_KEYS.values():
        _name, from_value, to_value = detail
        return {"newest_value": from_value, "rendered_value": to_value}
    return _change_entry(kind, detail)


def _same_but_for_app_image(response: Any, rendered: dict[str, Any]) -> bool:
    """Whether a registered revision equals the rendering, app image aside."""

    return _same_but_for("app_image", response, rendered)


def app_image_change(
    registered_response: Any, rendered: dict[str, Any]
) -> tuple[str, str] | None:
    """(registered image, rendered image) of an app-image-only difference.

    A registered revision differs from the rendering in the app image alone
    when both app images are pinned by digest, with no tag
    (``<account ECR repository URI>@sha256:<digest>``), in the same repository,
    the two digests differ, and with the rendered image put in its place the
    revision compares equal to the rendering (every other field, name, value,
    secret reference, the worker flag, the otel image, the roles and the tags).
    Anything else is None.
    """

    try:
        registered = _request_from_response(registered_response)
        registered_app = evidence._container_map(registered)[APP_CONTAINER]
        rendered_app = evidence._container_map(rendered)[APP_CONTAINER]
    except (WiringError, evidence.EvidenceError):
        return None
    from_image = registered_app.get("image")
    to_image = rendered_app.get("image")
    if not (
        isinstance(from_image, str)
        and isinstance(to_image, str)
        and _ECR_DIGEST_REFERENCE.fullmatch(from_image) is not None
        and _ECR_DIGEST_REFERENCE.fullmatch(to_image) is not None
        and from_image.split("@", 1)[0] == to_image.split("@", 1)[0]
        and from_image != to_image
    ):
        return None
    registered_app["image"] = to_image
    if _comparable(registered) != _comparable(rendered):
        return None
    return from_image, to_image


def _image_change_info(change: tuple[str, str]) -> dict[str, str]:
    """The repository and the two digests of an ``app_image_change``."""

    repository, from_digest = change[0].split("@", 1)
    return {
        "repository": repository,
        "from": from_digest,
        "to": change[1].split("@", 1)[1],
    }


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


def _deployed_kinds(revision: _Step) -> list[tuple[str, str, Any]]:
    """(kind, where, entry) of each reviewed change the plan deploys: proposed
    (a register_task_definition change) or registered and not deployed (the
    newest revision, reported in the revision step's info)."""

    kinds: list[tuple[str, str, Any]] = []
    for change in revision.changes:
        for kind in _SERVING_INFO_KEYS:
            if kind in change:
                kinds.append((kind, "proposed", change[kind]))
    for kind, key in _SERVING_INFO_KEYS.items():
        if key in revision.info:
            kinds.append((kind, "registered", revision.info[key]))
    return kinds


# The kinds of change the rollback target never gates (Decision 16): a flag
# flip (the worker, the credit switch, the transport) and the AppConfig group.
ROLLBACK_UNGATED_KINDS = (*FLAG_CHANGE_KEYS.values(), "appconfig_group_added")


def _deploys_only_a_flip(revision: _Step) -> bool:
    """Whether the plan deploys a change and every one is a flip kind."""

    kinds = _deployed_kinds(revision)
    return bool(kinds) and all(kind in ROLLBACK_UNGATED_KINDS for kind, *_ in kinds)


def _disabling_flip(revision: _Step) -> dict[str, Any] | None:
    """The one flag flip the plan deploys when it turns a flag off: its
    ``from`` (the serving or newest revision's value) is exactly 'true' and
    its ``to`` (the rendering's) exactly 'false'. None for anything else: an
    enabling flip, any other value, an image, the rows, the AppConfig group,
    nothing to deploy or more than one change (the direction cannot be
    determined: fail closed)."""

    kinds = _deployed_kinds(revision)
    if len(kinds) != 1:
        return None
    kind, where, entry = kinds[0]
    if (
        kind in FLAG_CHANGE_KEYS.values()
        and isinstance(entry, dict)
        and entry.get("from") == "true"
        and entry.get("to") == "false"
    ):
        return {"kind": kind, "change": where, "from": "true", "to": "false"}
    return None


def _plan_rollback_target(
    deployment: _Step, revision: _Step, readback: dict[str, Any], pins: ManifestPins
) -> None:
    """The manifest's rollback target: where a failed deployment goes back to.

    An unusable target (not ``ACTIVE``, unreadable, or not the manifest's
    revision pinned by digest) blocks every deploy except containment (owner
    decision, 2026-11-01): a flip of the worker, the credit switch or the
    transport from 'true' to 'false' (_disabling_flip). Enabling, an image,
    the staged rows and the AppConfig group wait for the target's repair."""

    deployment.info["rollback_target"] = pins.rollback_task_definition_arn
    deployment.info["rollback_target_effective_after"] = pins.rollback_effective_after
    deployment.info["rollback_target_must_repin_before"] = pins.rollback_repin_before
    response = _rollback_target_response(readback, pins)
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
        containment = _disabling_flip(revision)
        if containment is None:
            deployment.blocks.append("rollback_target_revision_unusable")
        else:
            deployment.info["rollback_target_unusable_containment_flip"] = containment


def _serving_image_moved_off_rollback_target(
    readback: dict[str, Any], service: dict[str, Any] | None, pins: ManifestPins
) -> bool:
    """Whether the service runs an app image other than the rollback target's.

    True when the rollback target's app image is pinned by digest and the
    service runs another revision whose app image is any other reference
    (another digest, another repository or a tag): an image change that
    already serves. Revision 8, pinned by tag, has no digest to compare with
    and never counts.
    """

    current = service.get("taskDefinition") if isinstance(service, dict) else None
    if (
        pins.rollback_revision == D3_SOURCE_REVISION
        or not isinstance(current, str)
        or current == pins.rollback_task_definition_arn
    ):
        return False
    serving_response = (
        readback.get("latest_task_definition")
        if current == _task_definition_arn_of(readback.get("latest_task_definition"))
        else readback.get("serving_task_definition")
    )
    target = (
        _source_app_container(readback.get("rollback_task_definition")) or {}
    ).get("image")
    serving = (_source_app_container(serving_response) or {}).get("image")
    return (
        isinstance(target, str)
        and isinstance(serving, str)
        and _ECR_DIGEST_REFERENCE.fullmatch(target) is not None
        and target != serving
    )


def _rollback_target_response(readback: dict[str, Any], pins: ManifestPins) -> Any:
    """The DescribeTaskDefinition readback of the manifest's rollback target."""

    if pins.rollback_revision == D3_SOURCE_REVISION:
        return readback.get("source_task_definition")
    return readback.get("rollback_task_definition")


def _serving_revision_response(readback: dict[str, Any], current: str) -> Any:
    """The read holding the revision the service runs, other than the D3
    source and the rollback target (the newest, or the serving read); None
    when no read holds exactly that revision."""

    latest = readback.get("latest_task_definition")
    response = (
        latest
        if current == _task_definition_arn_of(latest)
        else readback.get("serving_task_definition")
    )
    return response if _task_definition_arn_of(response) == current else None


def _staged_row_names(response: Any) -> set[str]:
    """The staged flag names the app environment of a readback carries."""

    environment = (_source_app_container(response) or {}).get("environment")
    rows = environment if isinstance(environment, list) else []
    return {
        str(row.get("name"))
        for row in rows
        if isinstance(row, dict) and row.get("name") in STAGED_FLAG_PARAMETERS
    }


def _serving_rows_off_rollback_target(
    readback: dict[str, Any], service: dict[str, Any] | None, pins: ManifestPins
) -> Any:
    """The serving revision's readback when it carries a staged flag row and
    the rollback target carries none: a rows change (L2a) that already
    serves. None otherwise; with the target already on a revision with the
    rows there is no rows change in service. Revision 8 never counts, as for
    the image (_serving_image_moved_off_rollback_target)."""

    current = service.get("taskDefinition") if isinstance(service, dict) else None
    if (
        pins.rollback_revision == D3_SOURCE_REVISION
        or not isinstance(current, str)
        or current == pins.rollback_task_definition_arn
    ):
        return None
    serving = _serving_revision_response(readback, current)
    if serving is None or not _staged_row_names(serving):
        return None
    if _staged_row_names(_rollback_target_response(readback, pins)):
        return None
    return serving


def _request_or_none(response: Any) -> dict[str, Any] | None:
    try:
        return _request_from_response(response)
    except WiringError:
        return None


# The two kinds of change the rollback target gates (Group 5b content rule)
# and the gate block each one reports. Flag flips (worker, credit switch,
# transport) and the AppConfig group are never gated by the target: a
# containment flip must always be deployable (Decision 16), so with a flip
# deployed the changes in service only report (_plan_rollback_content_rule).
ROLLBACK_GATED_KINDS: Mapping[str, str] = {
    "app_image": "rollback_target_not_repinned_before_image_bump",
    "flag_rows_added": "rollback_target_not_repinned_before_flag_rows",
}


def _gated_changes(
    revision: _Step,
    readback: dict[str, Any],
    service: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    pins: ManifestPins,
) -> list[tuple[str, str, dict[str, Any] | None]]:
    """(kind, where, request of the change's revision) of every gated change
    the plan sees: proposed (the rendering), registered and not deployed
    (the newest revision) or in service (the revision the service runs,
    relative to the rollback target)."""

    gated: list[tuple[str, str, dict[str, Any] | None]] = []
    for change in revision.changes:
        for kind in ROLLBACK_GATED_KINDS:
            if kind in change:
                gated.append((kind, "proposed", candidate))
    for kind in ROLLBACK_GATED_KINDS:
        if _SERVING_INFO_KEYS[kind] in revision.info:
            gated.append(
                (
                    kind,
                    "registered",
                    _request_or_none(readback.get("latest_task_definition")),
                )
            )
    current = service.get("taskDefinition") if isinstance(service, dict) else None
    if isinstance(current, str) and _serving_image_moved_off_rollback_target(
        readback, service, pins
    ):
        gated.append(
            (
                "app_image",
                "serving",
                _request_or_none(_serving_revision_response(readback, current)),
            )
        )
    rows = _serving_rows_off_rollback_target(readback, service, pins)
    if rows is not None:
        gated.append(("flag_rows_added", "serving", _request_or_none(rows)))
    return gated


def _rollback_content_failures(
    target_response: Any,
    request: dict[str, Any] | None,
    kind: str,
    pins: ManifestPins,
) -> list[str]:
    """Why the rollback target is not the way back from this change: [] when
    it is.

    The content rule (asaas-rollout-opening Group 5b): the target the
    manifest names, as read by DescribeTaskDefinition, is ACTIVE with its app
    image pinned by digest, carries exactly one BILLING_FACT_WORKER_ENABLED
    row and that row is 'true' (a way back never turns the worker off), and
    differs from the change's revision in that change's kind alone
    (reviewed_change); a target with a pending safety re-pin never is. The
    caller adds, for a proposed or registered change, that the service runs
    the target itself: with both, the target is the revision the change
    replaces.
    """

    failures: list[str] = []
    if pins.rollback_repin_before is not None:
        failures.append("target_repin_pending")
    definition = (
        target_response.get("taskDefinition")
        if isinstance(target_response, dict)
        else None
    )
    app = _source_app_container(target_response) or {}
    image = app.get("image")
    if not (
        isinstance(definition, dict)
        and definition.get("taskDefinitionArn") == pins.rollback_task_definition_arn
        and definition.get("status") == "ACTIVE"
        and isinstance(image, str)
        and _ECR_DIGEST_REFERENCE.fullmatch(image) is not None
    ):
        failures.append("target_not_active_and_digest_pinned")
    environment = app.get("environment")
    workers = [
        row
        for row in (environment if isinstance(environment, list) else [])
        if isinstance(row, dict) and row.get("name") == WORKER_FLAG_PARAMETER
    ]
    if not (len(workers) == 1 and workers[0].get("value") == "true"):
        failures.append("target_worker_flag_not_exactly_one_true")
    change = reviewed_change(target_response, request) if request is not None else None
    if change is None or change[0] != kind:
        failures.append("target_differs_from_the_change_in_more_than_its_kind")
    return failures


def _plan_rollback_content_rule(
    deployment: _Step,
    revision: _Step,
    readback: dict[str, Any],
    service: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    pins: ManifestPins,
) -> None:
    """Gate every app image and staged-rows change on the rollback target
    being the revision it replaces (_rollback_content_failures, plus the
    service on the target for a change not yet serving).

    When the plan deploys a flip (ROLLBACK_UNGATED_KINDS) the changes in
    service are still reported, with ``"gating": false``, and never block:
    a containment flip is always deployable (Decision 16), wherever the
    target is. A target with a pending safety re-pin (9, the worker 'false')
    still gates them, as before."""

    target_response = _rollback_target_response(readback, pins)
    current = service.get("taskDefinition") if isinstance(service, dict) else None
    flip = _deploys_only_a_flip(revision) and pins.rollback_repin_before is None
    verdicts: list[dict[str, Any]] = []
    for kind, where, request in _gated_changes(
        revision, readback, service, candidate, pins
    ):
        failures = _rollback_content_failures(target_response, request, kind, pins)
        if where != "serving" and current != pins.rollback_task_definition_arn:
            failures.append("service_not_on_target")
        verdict: dict[str, Any] = {"kind": kind, "change": where, "failed": failures}
        gating = not (flip and where == "serving")
        if not gating:
            verdict["gating"] = False
        verdicts.append(verdict)
        reason = ROLLBACK_GATED_KINDS[kind]
        if failures and gating and reason not in deployment.blocks:
            deployment.blocks.append(reason)
    if verdicts:
        deployment.info["rollback_target_content_rule"] = verdicts


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

    for window in MIGRATION_WINDOWS.values():
        steps[window.manifest_step].info.update(
            {
                "window": window.name,
                "alembic_version": (
                    "not readable through AWS APIs; read back with a SELECT-only "
                    "read task before and after the window"
                ),
                "required_current_revision": window.before,
                "command": window.command_text,
                "target_revision": window.after,
                "verification": window.verification,
                "snapshot_name_pattern": window.snapshot_pattern,
                "pre_read": window.pre_read.sql_file,
                "image_prerequisite": window.image_rule,
                "run_tooling": (
                    f"scripts/asaas_migration_window.py --window {window.name}"
                ),
                "plan_steps": dict(window.plan_steps),
                "excluded_revisions": MIGRATIONS_EXCLUDED[window.name],
            }
        )

    deployment = steps["service_deployment"]
    _plan_rollback_target(deployment, steps["task_definition_revision"], readback, pins)
    # Each structural deploy (an image change, the staged rows) goes back to
    # the revision it replaces, re-pinned one reviewed PR at a time: a
    # proposed, registered or serving change of either kind passes only when
    # the rollback target is that revision, by content. With the target 9
    # (the worker 'false'), 8 (a tag) or a revision the change does not
    # replace, the way back would turn the worker off, run older code or
    # undo more than the change.
    _plan_rollback_content_rule(
        deployment,
        steps["task_definition_revision"],
        readback,
        service,
        candidate,
        pins,
    )
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
    # Two stages (Decisions 12 and 14). Until live step L7 the rendering
    # carries no APPCONFIG_* name: every read of financial_egress_enabled
    # returns false before any I/O (closed by absence). From L7 on it carries
    # exactly the complete AppConfig group (source sdk), naming an AppConfig
    # application that exists; opening egress is then that configuration's
    # value, set outside this planner under its own approval. The D3 source
    # revision (labelled rollback_target since D3) never carries any
    # APPCONFIG_* name, and neither stage accepts a partial or altered group
    # or any other APPCONFIG_* name. The stage is the rendering's, but egress
    # is what the SERVING revision reads: closed by absence is reported only
    # when the revision the service runs carries no APPCONFIG_* name either
    # (params nulled while the serving task still carries the group is not
    # closed, and blocks).
    group = None
    serving_request, serving_stage = _serving_request_for_egress(
        readback, service, pins
    )
    egress.info["serving_stage"] = serving_stage
    for label, request in (
        ("rollback_target", source_request),
        (evaluated_on, candidate),
        ("serving", serving_request),
    ):
        if request is None:
            continue
        names = appconfig_names_in(request)
        values = appconfig_group_values(request)
        if not names:
            egress.compliant.append(f"no_appconfig_names_in_{label}")
        elif (
            label != "rollback_target"
            and values is not None
            and names == sorted(APPCONFIG_GROUP)
        ):
            if label == "serving":
                if group is None:
                    egress.blocks.append("appconfig_group_serving_not_in_rendering")
                else:
                    egress.compliant.append("appconfig_group_complete_in_serving")
            else:
                group = values
                egress.compliant.append(f"appconfig_group_complete_in_{label}")
        else:
            egress.blocks.append(f"appconfig_names_in_{label}")
    if serving_stage == "unreadable":
        egress.blocks.append("serving_task_definition_unreadable")
    response = readback.get("appconfig_applications")
    applications = response.get("Items") if isinstance(response, dict) else None
    readable = (
        _error_code(response) is None
        and isinstance(applications, list)
        and not (response or {}).get("NextToken")
    )
    egress.info["appconfig_application_count"] = (
        len(applications)
        if readable and isinstance(applications, list)
        else "unreadable"
    )
    if group is None and serving_stage == "closed_by_absence":
        egress.info["stage"] = "closed_by_absence"
        egress.info["financial_egress_enabled"] = "false (closed by absence)"
    elif group is None:
        egress.info["stage"] = "undetermined"
        egress.info["financial_egress_enabled"] = (
            "undetermined: the rendering carries no APPCONFIG_* name but the "
            f"serving revision's stage is {serving_stage}, so egress is what "
            "the serving task reads, not absent"
        )
    else:
        application = group[APPCONFIG_APPLICATION_PARAMETER]
        egress.info["stage"] = "appconfig_group_sdk"
        egress.info["financial_egress_enabled"] = (
            "read by the app from AppConfig (source sdk); its value is the "
            "configuration's, set outside this planner"
        )
        egress.info["appconfig"] = _appconfig_entry(group)
        matches = [
            item
            for item in (applications or [])
            if isinstance(item, dict)
            and application in (item.get("Name"), item.get("Id"))
        ]
        if not readable:
            egress.blocks.append("appconfig_applications_unreadable")
        elif len(matches) == 1:
            egress.compliant.append(f"appconfig_application_exists:{application}")
        elif matches:
            # Two applications answer to this name (or one's name is the
            # other's ID): which one the SDK reads is not knowable here.
            egress.blocks.append(f"appconfig_application_ambiguous:{application}")
        else:
            egress.blocks.append(f"appconfig_application_not_found:{application}")
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
            if group is None
            else "the secrets need none; the AppConfig group's sdk reads use the "
            "task role, whose AppConfig grant is live step L7's own IAM change, "
            "outside this planner"
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
            name: parameters.get(name)
            for name in (*REQUIRED_PARAMETERS, IMAGE_PARAMETER, *APPCONFIG_PARAMETERS)
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
    window = MIGRATION_WINDOWS.get(args.window)
    _require(window is not None, "migrations_window_unknown")
    assert window is not None
    aws = _aws_from_args(args, transport_factory, digests)
    readback = collect_migrations_readback(aws, parameters)
    status, rendered = plan_migrations(
        readback,
        parameters,
        pins=manifest_pins(manifest),
        window=window,
        purpose=args.purpose,
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
            # Required, no default: a window is never rendered by accident.
            sub.add_argument(
                "--window", required=True, choices=tuple(MIGRATION_WINDOWS)
            )
            # Required, no default: ``migrate`` bakes the window's upgrade,
            # ``read`` (window 037) bakes its SELECT-only readback.
            sub.add_argument("--purpose", required=True, choices=MIGRATIONS_PURPOSES)
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
