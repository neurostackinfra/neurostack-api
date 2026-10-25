"""Group 9 D3/D4 production wiring: read-only planner, manifest and template.

Every test runs against injected fake AWS responses; nothing here reaches AWS.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import datetime
import json
import os
import re
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import asaas_migration_window as mw
from scripts import asaas_producer_rollout_evidence as evidence
from scripts import asaas_production_wiring as wiring
from scripts import verify_asaas_producer_image_rollout as rollout
from src.config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / wiring.MANIFEST_RELATIVE_PATH
PARAMS = ROOT / wiring.PARAMETERS_RELATIVE_PATH
TEMPLATE = ROOT / wiring.TEMPLATE_RELATIVE_PATH
SCRIPT = ROOT / "scripts" / "asaas_production_wiring.py"

ACCOUNT = "388460142431"
SECRET_PREFIX = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:neurostack/staging/"
API_KEY_ARN = SECRET_PREFIX + "asaas-api-key-AbC123"
WEBHOOK_TOKEN_ARN = SECRET_PREFIX + "asaas-webhook-token-DeF456"
# The smoke token created by the applied stack (2026-10-18), as the manifest records.
SMOKE_ARN = SECRET_PREFIX + "billing-observability-smoke-token-mSR8Ux"
CHANGE_SET_ID = (
    f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:changeSet/d3-wiring-review/"
    "ed966125-f34f-4829-aab1-e36b63d5aaaf"
)
APPLIED = {
    "date": "2026-10-18",
    "stack_name": "neurostack-asaas-production-wiring",
    "change_set_name": "d3-wiring-review",
    "execution_options": "RetainExceptOnCreate",
    "smoke_token_secret_arn": SMOKE_ARN,
    "terraform_policy_unchanged": True,
}
STACK_RESOURCES = [
    {
        "LogicalResourceId": "BillingObservabilitySmokeTokenSecret",
        "PhysicalResourceId": SMOKE_ARN,
        "ResourceType": "AWS::SecretsManager::Secret",
        "ResourceStatus": "CREATE_COMPLETE",
    },
    {
        "LogicalResourceId": "ExecutionRoleWiringSecretRead",
        "PhysicalResourceId": "neuro-Execu-2FtLqoMVADje",
        "ResourceType": "AWS::IAM::Policy",
        "ResourceStatus": "CREATE_COMPLETE",
    },
]
EXECUTION_ROLE = "neurostack-staging-ecs-execution"
TASK_ROLE = "neurostack-staging-ecs-task"
SOURCE_ARN = wiring.D3_SOURCE_TASK_DEFINITION_ARN
NEXT_ARN = SOURCE_ARN.rsplit(":", 1)[0] + ":9"
# The current :latest resolution, pinned deliberately (orchestrator default,
# 2026-10-19); the manifest field task_definition.otel_image holds it.
OTEL_DIGEST_IMAGE = (
    "public.ecr.aws/aws-observability/aws-otel-collector@sha256:"
    "7968fb60db6a2390a47ba6a2df029745638486e285c9b2487da1b722d0855a3e"
)
MIGRATIONS_SOURCE_ARN = (
    f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/neurostack-staging-migrations:3"
)
# Only the trailing revision changes (":3" also occurs in the account).
MIGRATIONS_NEXT_ARN = MIGRATIONS_SOURCE_ARN.rsplit(":", 1)[0] + ":4"
MIGRATIONS_DB_ARN = SECRET_PREFIX + "database-url-migrations-Mg7x2Q"
LIVE_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/neurostack-staging:"
    "asaas-gateway-staging-20261014-cfcb7f0b"
)
LIVE_DIGEST = "sha256:" + "1" * 64
OVERRIDE_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/neurostack-staging@sha256:" + "2" * 64
)
LB_ARN = (
    f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:loadbalancer/app/"
    "neurostack-staging-alb/0ce73c4cbe0d22b3"
)
HTTPS_LISTENER_ARN = (
    LB_ARN.replace(":loadbalancer/", ":listener/") + "/d4a38f72d11a3fc7"
)
HTTP_LISTENER_ARN = LB_ARN.replace(":loadbalancer/", ":listener/") + "/4b2cd405d66d2890"
TG_ARN = (
    f"arn:aws:elasticloadbalancing:us-east-1:{ACCOUNT}:targetgroup/"
    "neurostack-staging-app/e16d410e30217dd8"
)
CERT_ARN = (
    f"arn:aws:acm:us-east-1:{ACCOUNT}:certificate/34d44f51-7a81-426b-a2ed-613e88c5ca86"
)
ALB_DNS = "neurostack-staging-alb-476326481.us-east-1.elb.amazonaws.com"
ZONE_ID = "Z06847931V6B8WAIKCKFV"
# Built by concatenation so repository secret scanners never see a literal.
CANARY_KEY = "$" + "aact_prod_" + "Zq" * 12
CANARY_PEM = "-----" + "BEGIN PRIVATE KEY-----"
CANARY_TEXT = "canary-" + "do-not-print-7f3a"


def _secret(name: str, arn: str) -> dict[str, Any]:
    return {
        "ARN": arn,
        "Name": f"neurostack/staging/{name}",
        "Description": CANARY_TEXT,
        "KmsKeyId": wiring.KMS_KEY_ARN,
        "Tags": [{"Key": "Note", "Value": CANARY_TEXT}],
        "VersionIdsToStages": {"v1": ["AWSCURRENT"]},
    }


def _task_definition(
    arn: str = SOURCE_ARN,
    *,
    app_environment: list[dict[str, str]] | None = None,
    app_secrets: list[dict[str, str]] | None = None,
    image: str = LIVE_IMAGE,
) -> dict[str, Any]:
    revision = int(arn.rsplit(":", 1)[1])
    return {
        "taskDefinition": {
            "taskDefinitionArn": arn,
            "containerDefinitions": [
                {
                    "name": "app",
                    "image": image,
                    "cpu": 0,
                    "portMappings": [
                        {"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"}
                    ],
                    "essential": True,
                    "environment": (
                        app_environment
                        if app_environment is not None
                        else [
                            {"name": "LOG_FORMAT", "value": "json"},
                            {"name": "HOST", "value": "0.0.0.0"},
                            {"name": "PORT", "value": "8000"},
                            {"name": "ENVIRONMENT", "value": "staging"},
                            {"name": "LLM_MODEL", "value": "o4-mini"},
                            {"name": "QDRANT_HOST", "value": ""},
                        ]
                    ),
                    "mountPoints": [],
                    "volumesFrom": [],
                    "secrets": (
                        app_secrets
                        if app_secrets is not None
                        else [
                            {
                                "name": "DATABASE_URL",
                                "valueFrom": SECRET_PREFIX + "database-url-fyOYuj",
                            },
                            {
                                "name": "REDIS_URL",
                                "valueFrom": SECRET_PREFIX + "redis-url-4IP59p",
                            },
                            {
                                "name": "SECRET_KEY",
                                "valueFrom": SECRET_PREFIX + "secret-key-evo4z9",
                            },
                            {
                                "name": "LLM_API_KEY",
                                "valueFrom": SECRET_PREFIX + "llm-api-key-Egp931",
                            },
                            {
                                "name": "BILLING_OPERATION_RECEIPT_SIGNING_KEY",
                                "valueFrom": SECRET_PREFIX
                                + "billing-operation-receipt-signing-key-3BxFTN",
                            },
                        ]
                    ),
                    "stopTimeout": 30,
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {"awslogs-group": "/ecs/neurostack-staging/app"},
                    },
                    "systemControls": [],
                },
                {
                    "name": "otel",
                    "image": "public.ecr.aws/aws-observability/aws-otel-collector:latest",
                    "cpu": 0,
                    "portMappings": [],
                    "essential": False,
                    "command": ["--config=/etc/ecs/ecs-cloudwatch.yaml"],
                    "environment": [],
                    "mountPoints": [],
                    "volumesFrom": [],
                    "systemControls": [],
                },
            ],
            "family": "neurostack-staging-app",
            "taskRoleArn": evidence.TASK_ROLE_ARN,
            "executionRoleArn": evidence.EXECUTION_ROLE_ARN,
            "networkMode": "awsvpc",
            "revision": revision,
            "volumes": [],
            "status": "ACTIVE",
            "requiresAttributes": [
                {"name": "ecs.capability.secrets.asm.environment-variables"}
            ],
            "placementConstraints": [],
            "compatibilities": ["EC2", "FARGATE"],
            "runtimePlatform": {
                "cpuArchitecture": "X86_64",
                "operatingSystemFamily": "LINUX",
            },
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "1024",
            "memory": "2048",
            "registeredAt": "2026-10-14T22:52:39.288000+00:00",
            "registeredBy": f"arn:aws:iam::{ACCOUNT}:user/operator",
        },
        "tags": [
            {"key": "Project", "value": "neurostack-api"},
            {"key": "Environment", "value": "staging"},
        ],
    }


def _migrations_task_definition(
    arn: str = MIGRATIONS_SOURCE_ARN,
    *,
    containers: list[dict[str, Any]] | None = None,
    **container_changes: Any,
) -> dict[str, Any]:
    """DescribeTaskDefinition of the one-off migrations family (revision 3 shape)."""

    revision = int(arn.rsplit(":", 1)[1])
    container: dict[str, Any] = {
        "name": "migrations",
        "image": LIVE_IMAGE,
        "cpu": 0,
        "essential": True,
        # What revision 3 bakes: the unpinned upgrade the window forbids.
        "command": ["alembic", "upgrade", "he" + "ad"],
        "environment": [{"name": "ENVIRONMENT", "value": "staging"}],
        "secrets": [{"name": "DATABASE_URL", "valueFrom": MIGRATIONS_DB_ARN}],
        "mountPoints": [],
        "volumesFrom": [],
        "systemControls": [],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "/ecs/neurostack-staging/migrations",
                "awslogs-region": "us-east-1",
                "awslogs-stream-prefix": "ecs",
            },
        },
    }
    container.update(container_changes)
    return {
        "taskDefinition": {
            "taskDefinitionArn": arn,
            "containerDefinitions": (
                containers if containers is not None else [container]
            ),
            "family": "neurostack-staging-migrations",
            "taskRoleArn": evidence.TASK_ROLE_ARN,
            "executionRoleArn": evidence.EXECUTION_ROLE_ARN,
            "networkMode": "awsvpc",
            "revision": revision,
            "volumes": [],
            "status": "ACTIVE",
            "requiresAttributes": [
                {"name": "ecs.capability.secrets.asm.environment-variables"}
            ],
            "placementConstraints": [],
            "compatibilities": ["EC2", "FARGATE"],
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "512",
            "memory": "1024",
            "registeredAt": "2026-10-10T20:00:00.000000+00:00",
            "registeredBy": f"arn:aws:iam::{ACCOUNT}:user/operator",
        },
        "tags": [
            {"key": "ManagedBy", "value": "terraform"},
            {"key": "Project", "value": "neurostack-api"},
        ],
    }


def _service(task_definition: str = SOURCE_ARN, **changes: Any) -> dict[str, Any]:
    service: dict[str, Any] = {
        "serviceArn": wiring.rollout.SERVICE_ARN,
        "serviceName": "neurostack-staging-app",
        "status": "ACTIVE",
        "desiredCount": 2,
        "runningCount": 2,
        "taskDefinition": task_definition,
        "loadBalancers": [
            {"targetGroupArn": TG_ARN, "containerName": "app", "containerPort": 8000}
        ],
        "deploymentConfiguration": {
            "deploymentCircuitBreaker": {"enable": True, "rollback": True},
            "maximumPercent": 200,
            "minimumHealthyPercent": 100,
        },
        "deployments": [
            {
                "id": "ecs-svc/1",
                "status": "PRIMARY",
                "taskDefinition": task_definition,
                "rolloutState": "COMPLETED",
            }
        ],
    }
    service.update(changes)
    return {"services": [service], "failures": []}


def _policy(document: dict[str, Any], name: str) -> dict[str, Any]:
    return {"RoleName": EXECUTION_ROLE, "PolicyName": name, "PolicyDocument": document}


TERRAFORM_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "LerOsSegredosDaAplicacao",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": [SECRET_PREFIX + "database-url-fyOYuj"],
        },
        {
            "Sid": "DecifrarSegredoEImagem",
            "Effect": "Allow",
            "Action": "kms:Decrypt",
            "Resource": wiring.KMS_KEY_ARN,
        },
    ],
}


class FakeAws:
    """In-memory AWS: exact request -> response, with a call log."""

    def __init__(self, *, applied: bool = False) -> None:
        self.responses: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.stack_applied = False
        self.put("sts", "GetCallerIdentity", {}, {"Account": ACCOUNT, "Arn": "arn"})
        self.put(
            "ecs",
            "DescribeServices",
            {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
            _service(),
        )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-app:8", "include": ["TAGS"]},
            _task_definition(),
        )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-app", "include": ["TAGS"]},
            _task_definition(),
        )
        self.put(
            "iam",
            "GetRole",
            {"RoleName": EXECUTION_ROLE},
            {"Role": {"Arn": evidence.EXECUTION_ROLE_ARN, "Description": CANARY_TEXT}},
        )
        self.put(
            "iam",
            "ListRolePolicies",
            {"RoleName": EXECUTION_ROLE},
            {"PolicyNames": ["segredos-e-logs"]},
        )
        self.put(
            "iam",
            "ListAttachedRolePolicies",
            {"RoleName": EXECUTION_ROLE},
            {
                "AttachedPolicies": [
                    {
                        "PolicyName": "AmazonECSTaskExecutionRolePolicy",
                        "PolicyArn": wiring.EXECUTION_MANAGED_POLICY_ARN,
                    }
                ]
            },
        )
        self.put(
            "iam",
            "GetRolePolicy",
            {"RoleName": EXECUTION_ROLE, "PolicyName": "segredos-e-logs"},
            _policy(TERRAFORM_POLICY, "segredos-e-logs"),
        )
        self.put(
            "iam",
            "GetRolePolicy",
            {"RoleName": EXECUTION_ROLE, "PolicyName": wiring.WIRING_POLICY_NAME},
            {"__error_code__": "NoSuchEntity"},
        )
        self.put(
            "iam",
            "ListRolePolicies",
            {"RoleName": TASK_ROLE},
            {"PolicyNames": ["runtime-da-aplicacao"]},
        )
        self.put(
            "iam",
            "ListAttachedRolePolicies",
            {"RoleName": TASK_ROLE},
            {"AttachedPolicies": []},
        )
        self.put(
            "secretsmanager",
            "DescribeSecret",
            {"SecretId": "neurostack/staging/asaas-api-key"},
            _secret("asaas-api-key", API_KEY_ARN),
        )
        self.put(
            "secretsmanager",
            "DescribeSecret",
            {"SecretId": "neurostack/staging/asaas-webhook-token"},
            _secret("asaas-webhook-token", WEBHOOK_TOKEN_ARN),
        )
        self.put(
            "secretsmanager",
            "DescribeSecret",
            {"SecretId": "neurostack/staging/billing-observability-smoke-token"},
            {"__error_code__": "ResourceNotFoundException"},
        )
        self.put(
            "cloudformation",
            "DescribeStacks",
            {"StackName": wiring.STACK_NAME},
            {"__error_code__": "ValidationError"},
        )
        self.put("appconfig", "ListApplications", {}, {"Items": []})
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-migrations:3", "include": ["TAGS"]},
            _migrations_task_definition(),
        )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-migrations", "include": ["TAGS"]},
            _migrations_task_definition(),
        )
        self.put(
            "ecr",
            "DescribeImages",
            {
                "repositoryName": "neurostack-staging",
                "imageIds": [{"imageTag": "asaas-gateway-staging-20261014-cfcb7f0b"}],
            },
            {"imageDetails": [{"imageDigest": LIVE_DIGEST}]},
        )
        self.put(
            "ecr",
            "DescribeImages",
            {
                "repositoryName": "neurostack-staging",
                "imageIds": [{"imageDigest": "sha256:" + "2" * 64}],
            },
            {"imageDetails": [{"imageDigest": "sha256:" + "2" * 64}]},
        )
        self.put(
            "elbv2",
            "DescribeLoadBalancers",
            {"Names": ["neurostack-staging-alb"]},
            {"LoadBalancers": [{"LoadBalancerArn": LB_ARN, "DNSName": ALB_DNS}]},
        )
        self.put(
            "elbv2",
            "DescribeListeners",
            {"LoadBalancerArn": LB_ARN},
            {
                "Listeners": [
                    {
                        "ListenerArn": HTTP_LISTENER_ARN,
                        "Port": 80,
                        "Protocol": "HTTP",
                        "DefaultActions": [{"Type": "redirect"}],
                    },
                    {
                        "ListenerArn": HTTPS_LISTENER_ARN,
                        "Port": 443,
                        "Protocol": "HTTPS",
                        "Certificates": [{"CertificateArn": CERT_ARN}],
                        "DefaultActions": [
                            {"Type": "forward", "TargetGroupArn": TG_ARN}
                        ],
                    },
                ]
            },
        )
        self.put(
            "elbv2",
            "DescribeRules",
            {"ListenerArn": HTTPS_LISTENER_ARN},
            {"Rules": [{"Priority": "default", "IsDefault": True, "Conditions": []}]},
        )
        self.put(
            "acm",
            "DescribeCertificate",
            {"CertificateArn": CERT_ARN},
            {
                "Certificate": {
                    "Status": "ISSUED",
                    "SubjectAlternativeNames": [
                        "staging.api.neurostack.global",
                        "*.staging.api.neurostack.global",
                    ],
                }
            },
        )
        self.put(
            "wafv2",
            "GetWebACLForResource",
            {"ResourceArn": LB_ARN},
            {
                "WebACL": {
                    "Name": "neurostack-staging",
                    "DefaultAction": {"Allow": {}},
                    "Rules": [
                        {
                            "Name": "AWSManagedRulesCommonRuleSet",
                            "OverrideAction": {"Count": {}},
                        },
                        {"Name": "rate-limit-por-ip", "Action": {"Count": {}}},
                    ],
                }
            },
        )
        self.put(
            "route53",
            "ListHostedZonesByName",
            {"DNSName": "api.neurostack.global"},
            {
                "HostedZones": [
                    {
                        "Id": f"/hostedzone/{ZONE_ID}",
                        "Name": "api.neurostack.global.",
                        "Config": {"PrivateZone": False},
                    }
                ]
            },
        )
        self.put(
            "route53",
            "ListResourceRecordSets",
            {"HostedZoneId": ZONE_ID},
            {
                "ResourceRecordSets": [
                    {
                        "Name": f"{host}.",
                        "Type": "A",
                        "AliasTarget": {"DNSName": f"{ALB_DNS}."},
                    }
                    for host in wiring.SERVED_HOSTS
                ]
            },
        )
        if applied:
            self.apply_stack()

    def put(
        self, service: str, operation: str, params: dict[str, Any], response: Any
    ) -> None:
        self.responses[wiring._read_key(service, operation, params)] = response

    def get(self, service: str, operation: str, params: dict[str, Any]) -> Any:
        return self.responses[wiring._read_key(service, operation, params)]

    def apply_stack(
        self,
        *,
        policy_arns: list[str] | None = None,
        policy_document: dict[str, Any] | None = None,
        policy_present: bool = True,
        status: str = "CREATE_COMPLETE",
        parameters: dict[str, str] | None = None,
        resources: list[dict[str, Any]] | None = None,
        template_body: str | None = None,
        change_set_id: str = CHANGE_SET_ID,
    ) -> None:
        self.stack_applied = True
        self.put(
            "secretsmanager",
            "DescribeSecret",
            {"SecretId": "neurostack/staging/billing-observability-smoke-token"},
            _secret("billing-observability-smoke-token", SMOKE_ARN),
        )
        stack_parameters = parameters or {
            "AsaasApiKeySecretArn": API_KEY_ARN,
            "AsaasWebhookTokenSecretArn": WEBHOOK_TOKEN_ARN,
        }
        self.put(
            "cloudformation",
            "DescribeStacks",
            {"StackName": wiring.STACK_NAME},
            {
                "Stacks": [
                    {
                        "StackName": wiring.STACK_NAME,
                        "StackStatus": status,
                        "ChangeSetId": change_set_id,
                        "Parameters": [
                            {"ParameterKey": key, "ParameterValue": value}
                            for key, value in stack_parameters.items()
                        ],
                        "Outputs": [{"OutputKey": "x", "OutputValue": CANARY_TEXT}],
                    }
                ]
            },
        )
        self.put(
            "cloudformation",
            "ListStackResources",
            {"StackName": wiring.STACK_NAME},
            {
                "StackResourceSummaries": copy.deepcopy(
                    STACK_RESOURCES if resources is None else resources
                )
            },
        )
        self.put(
            "cloudformation",
            "GetTemplate",
            {"StackName": wiring.STACK_NAME, "TemplateStage": "Original"},
            {
                "TemplateBody": (
                    TEMPLATE.read_text(encoding="utf-8")
                    if template_body is None
                    else template_body
                ),
                "StagesAvailable": ["Original", "Processed"],
            },
        )
        self.put(
            "iam",
            "ListRolePolicies",
            {"RoleName": EXECUTION_ROLE},
            {"PolicyNames": ["segredos-e-logs", wiring.WIRING_POLICY_NAME]},
        )
        document = policy_document or wiring.expected_wiring_policy(
            policy_arns or [API_KEY_ARN, WEBHOOK_TOKEN_ARN, SMOKE_ARN]
        )
        self.put(
            "iam",
            "GetRolePolicy",
            {"RoleName": EXECUTION_ROLE, "PolicyName": wiring.WIRING_POLICY_NAME},
            (
                _policy(document, wiring.WIRING_POLICY_NAME)
                if policy_present
                else {"__error_code__": "NoSuchEntity"}
            ),
        )

    def transport(self, service: str, operation: str, params: dict[str, Any]) -> Any:
        self.calls.append((service, operation, params))
        key = wiring._read_key(service, operation, params)
        if key not in self.responses:
            raise AssertionError(f"unexpected request {service}.{operation} {params}")
        return copy.deepcopy(self.responses[key])

    def snapshot(self, path: Path) -> Path:
        reads = []
        for key, response in self.responses.items():
            service, operation, params = json.loads(key)
            reads.append(
                {
                    "service": service,
                    "operation": operation,
                    "params": params,
                    "response": response,
                }
            )
        path.write_text(
            json.dumps(
                {
                    "schema_version": wiring.READBACK_SCHEMA,
                    "account_id": ACCOUNT,
                    "region": "us-east-1",
                    "captured_at_utc": "2026-10-18T00:00:00Z",
                    "reads": reads,
                }
            ),
            encoding="utf-8",
        )
        return path


def _parameters(**changes: Any) -> dict[str, Any]:
    raw = yaml.safe_load(PARAMS.read_text(encoding="utf-8"))
    raw["parameters"].update(changes)
    return raw


def _validated(**changes: Any) -> dict[str, Any]:
    return wiring.validate_parameters(_parameters(**changes))


def _pins(manifest: dict[str, Any] | None = None) -> Any:
    return wiring.manifest_pins(
        wiring.validate_manifest(manifest or wiring.load_yaml(MANIFEST))
    )


# The rollback target as the manifest held it from the D3 planning of
# 2026-10-18 until its re-pin to revision 9 (the planner still accepts it).
INITIAL_ROLLBACK_TARGET: dict[str, Any] = {
    "revision": 8,
    "effective_after": "initial_d3_planning_2026_10_18",
    "repin": {
        "revision": 9,
        "effective_after": (
            "bridge_revision_9_serving_verified_and_observation_closed"
        ),
        "before": "migration_036",
        "why": "Revision 8 reads legacy columns that migration 036 drops.",
    },
}


def _initial_manifest() -> dict[str, Any]:
    """The reviewed manifest with the rollback target it held before the re-pin."""

    manifest: dict[str, Any] = wiring.load_yaml(MANIFEST)
    manifest["rollback_target"] = copy.deepcopy(INITIAL_ROLLBACK_TARGET)
    return manifest


def _initial_pins() -> Any:
    return _pins(_initial_manifest())


def _initial_manifest_file(tmp_path: Path) -> Path:
    path = tmp_path / "manifest-initial.yaml"
    path.write_text(yaml.safe_dump(_initial_manifest()), encoding="utf-8")
    return path


def _plan(
    fake: FakeAws,
    *,
    applied: dict[str, Any] | None = None,
    pins: Any = None,
    **changes: Any,
) -> tuple[dict[str, Any], Any]:
    parameters = _validated(**changes)
    # The fixture world is the D3 world of 2026-10-18 (the service on revision
    # 8, revision 9 not yet registered), planned against the manifest of that
    # time: rollback_target 8 with its re-pin note. The reviewed manifest has
    # been re-pinned since (8 to 9, then 9 to 10); the tests below that plan
    # the later states pass the pins they need (_worker_pins).
    pins = pins if pins is not None else _initial_pins()
    aws = wiring.ReadOnlyAws(fake.transport)
    readback = wiring.collect_readback(aws, parameters, pins=pins)
    return wiring.build_plan(
        parameters,
        readback,
        {"template_sha256": "t" * 64, "manifest_sha256": "m"},
        # A pre-apply world is planned against the pre-apply manifest (no
        # record); an applied world against the record the manifest now holds.
        applied=(
            (APPLIED if fake.stack_applied else None) if applied is None else applied
        ),
        pins=pins,
    )


def _drifted_policy(kind: str) -> dict[str, Any]:
    document = wiring.expected_wiring_policy(
        [API_KEY_ARN, WEBHOOK_TOKEN_ARN, SMOKE_ARN]
    )
    statement = document["Statement"][0]
    if kind == "wildcard":
        statement["Resource"] = [SECRET_PREFIX + "*"]
    elif kind == "extra_action":
        statement["Action"] = [
            "secretsmanager:GetSecretValue",
            "secretsmanager:PutSecretValue",
        ]
    elif kind == "condition":
        statement["Condition"] = {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    elif kind == "not_resource":
        statement["NotResource"] = statement.pop("Resource")
    return document


def _smoke_outside_stack(fake: FakeAws) -> None:
    fake.put(
        "secretsmanager",
        "DescribeSecret",
        {"SecretId": "neurostack/staging/billing-observability-smoke-token"},
        _secret("billing-observability-smoke-token", SMOKE_ARN),
    )


# state -> (how to reach it, a block reason the plan must report)
BLOCKED_STATES: dict[str, tuple[Callable[[FakeAws], None], str]] = {
    "smoke_secret_outside_stack": (
        _smoke_outside_stack,
        "smoke_token_secret_exists_outside_stack",
    ),
    "stack_rollback_complete": (
        lambda f: f.apply_stack(status="ROLLBACK_COMPLETE"),
        "stack_status:ROLLBACK_COMPLETE",
    ),
    "wiring_policy_missing": (
        lambda f: f.apply_stack(policy_present=False),
        "stack_wiring_policy_missing",
    ),
    "policy_wildcard_resource": (
        lambda f: f.apply_stack(policy_document=_drifted_policy("wildcard")),
        "wiring_policy_drift:policy_wildcard_resource",
    ),
    "policy_extra_action": (
        lambda f: f.apply_stack(policy_document=_drifted_policy("extra_action")),
        "wiring_policy_drift:policy_action",
    ),
    "policy_condition": (
        lambda f: f.apply_stack(policy_document=_drifted_policy("condition")),
        "wiring_policy_drift:policy_statement_keys",
    ),
    "policy_not_resource": (
        lambda f: f.apply_stack(policy_document=_drifted_policy("not_resource")),
        "wiring_policy_drift:policy_statement_keys",
    ),
    "stack_parameters_drifted": (
        lambda f: f.apply_stack(
            parameters={
                "AsaasApiKeySecretArn": SECRET_PREFIX + "asaas-api-key-ZzZ999",
                "AsaasWebhookTokenSecretArn": WEBHOOK_TOKEN_ARN,
            }
        ),
        "stack_parameters_drift",
    ),
    "stack_resources_mismatch": (
        lambda f: f.apply_stack(
            resources=STACK_RESOURCES
            + [
                {
                    "LogicalResourceId": "Extra",
                    "PhysicalResourceId": "x",
                    "ResourceType": "AWS::IAM::Policy",
                    "ResourceStatus": "CREATE_COMPLETE",
                }
            ]
        ),
        "stack_resources_mismatch",
    ),
    "stack_template_differs": (
        lambda f: f.apply_stack(
            template_body=TEMPLATE.read_text(encoding="utf-8").replace(
                "- !Ref AsaasWebhookTokenSecretArn",
                "- !Ref AsaasWebhookTokenSecretArn\n              - '*'",
            )
        ),
        "stack_template_differs_from_reviewed_template",
    ),
}


def _step(plan: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(row for row in plan["steps"] if row["id"] == step_id)


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = wiring.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _write_params(tmp_path: Path, **changes: Any) -> Path:
    path = tmp_path / "params.yaml"
    path.write_text(yaml.safe_dump(_parameters(**changes)), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


def test_allowlist_contains_only_read_operations() -> None:
    assert wiring.ALLOWED_OPERATIONS
    for _service, operation in wiring.ALLOWED_OPERATIONS:
        assert operation.startswith(("Describe", "Get", "List"))
        assert operation not in wiring.FORBIDDEN_OPERATIONS
    assert ("secretsmanager", "GetSecretValue") not in wiring.ALLOWED_OPERATIONS
    assert ("secretsmanager", "BatchGetSecretValue") not in wiring.ALLOWED_OPERATIONS


@pytest.mark.parametrize(
    "service,operation",
    [
        ("secretsmanager", "GetSecretValue"),
        ("secretsmanager", "BatchGetSecretValue"),
        ("secretsmanager", "CreateSecret"),
        ("secretsmanager", "GetRandomPassword"),
        ("iam", "PutRolePolicy"),
        ("ecs", "RegisterTaskDefinition"),
        ("ecs", "UpdateService"),
        ("cloudformation", "CreateStack"),
        ("cloudformation", "ExecuteChangeSet"),
        ("appconfig", "CreateApplication"),
        ("ecs", "ListTasks"),
    ],
)
def test_guard_refuses_any_operation_outside_the_allowlist(
    service: str, operation: str
) -> None:
    reached: list[str] = []

    def transport(*_args: Any) -> dict[str, Any]:
        reached.append("called")
        return {}

    aws = wiring.ReadOnlyAws(transport)
    with pytest.raises(wiring.WiringError, match="aws_operation_not_allowed"):
        aws.call(service, operation, {})
    assert reached == []
    assert aws.calls == []


def test_allowlist_self_check_rejects_a_mutating_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widened = set(wiring.ALLOWED_OPERATIONS) | {("iam", "PutRolePolicy")}
    monkeypatch.setattr(wiring, "ALLOWED_OPERATIONS", frozenset(widened))
    with pytest.raises(wiring.WiringError, match="aws_allowlist_not_read_only"):
        wiring._assert_read_only_allowlist()
    secret_read = set(wiring.ALLOWED_OPERATIONS) | {
        ("secretsmanager", "GetSecretValue")
    }
    monkeypatch.setattr(wiring, "ALLOWED_OPERATIONS", frozenset(secret_read))
    with pytest.raises(wiring.WiringError, match="aws_allowlist_not_read_only"):
        wiring._assert_read_only_allowlist()


def test_plan_issues_only_allowlisted_reads() -> None:
    fake = FakeAws()
    _plan(fake)
    assert fake.calls
    assert {(s, o) for s, o, _p in fake.calls} <= wiring.ALLOWED_OPERATIONS
    assert all(o != "GetSecretValue" for _s, o, _p in fake.calls)


def test_guard_rejects_a_response_carrying_secret_material() -> None:
    aws = wiring.ReadOnlyAws(lambda *_args: {"ARN": "x", "SecretString": CANARY_KEY})
    with pytest.raises(wiring.WiringError, match="secret_material_in_readback"):
        aws.call("secretsmanager", "DescribeSecret", {"SecretId": "x"})


def test_boto3_transport_checks_the_allowlist_before_any_client_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked: list[str] = []

    class ClientError(Exception):
        def __init__(self, code: str) -> None:
            self.response = {"Error": {"Code": code, "Message": CANARY_TEXT}}

    class Client:
        def describe_secret(self, **_params: Any) -> dict[str, Any]:
            invoked.append("describe_secret")
            raise ClientError("ResourceNotFoundException")

        def get_secret_value(self, **_params: Any) -> dict[str, Any]:
            invoked.append("get_secret_value")
            return {"SecretString": CANARY_KEY}

    class Session:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def client(self, *_args: Any, **_kwargs: Any) -> Client:
            return Client()

    boto3 = types.ModuleType("boto3")
    boto3.session = types.SimpleNamespace(Session=Session)  # type: ignore[attr-defined]
    botocore = types.ModuleType("botocore")
    botocore.xform_name = lambda name: re.sub(  # type: ignore[attr-defined]
        r"(?<!^)(?=[A-Z])", "_", name
    ).lower()
    config = types.ModuleType("botocore.config")
    config.Config = lambda **_kwargs: None  # type: ignore[attr-defined]
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError  # type: ignore[attr-defined]
    exceptions.BotoCoreError = RuntimeError  # type: ignore[attr-defined]
    for name, module in (
        ("boto3", boto3),
        ("botocore", botocore),
        ("botocore.config", config),
        ("botocore.exceptions", exceptions),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    transport = wiring.boto3_transport()
    with pytest.raises(wiring.WiringError, match="aws_operation_not_allowed"):
        transport("secretsmanager", "GetSecretValue", {"SecretId": "x"})
    assert invoked == []
    response = transport("secretsmanager", "DescribeSecret", {"SecretId": "x"})
    assert response == {"__error_code__": "ResourceNotFoundException"}
    assert CANARY_TEXT not in json.dumps(response)
    assert invoked == ["describe_secret"]


def test_tool_source_has_no_mutating_sdk_or_shell_call() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(
        r"\.(?:put|create|update|register|delete|deregister|tag|untag|start|stop|"
        r"run|execute|get_secret_value|batch_get_secret_value)_\w*\(",
        source,
    )
    assert "subprocess" not in source
    assert "os.system" not in source


# ---------------------------------------------------------------------------
# IAM delta and secret creation
# ---------------------------------------------------------------------------


def test_template_grants_get_secret_value_on_exactly_the_three_secrets() -> None:
    template = wiring.validate_secret_access_template(wiring.load_yaml(TEMPLATE))
    policy = template["Resources"]["ExecutionRoleWiringSecretRead"]["Properties"]
    assert policy["Roles"] == [EXECUTION_ROLE]
    statements = policy["PolicyDocument"]["Statement"]
    assert len(statements) == 1
    assert statements[0]["Action"] == "secretsmanager:GetSecretValue"
    assert statements[0]["Resource"] == [
        {"Ref": "AsaasApiKeySecretArn"},
        {"Ref": "AsaasWebhookTokenSecretArn"},
        {"Ref": "BillingObservabilitySmokeTokenSecret"},
    ]
    assert "*" not in json.dumps(policy)
    secret = template["Resources"]["BillingObservabilitySmokeTokenSecret"]
    assert secret["Properties"]["KmsKeyId"] == wiring.KMS_KEY_ARN
    assert "SecretString" not in secret["Properties"]
    assert secret["DeletionPolicy"] == "Retain"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t["Resources"]["ExecutionRoleWiringSecretRead"]["Properties"][
            "PolicyDocument"
        ]["Statement"][0].__setitem__("Resource", "*"),
        lambda t: t["Resources"]["ExecutionRoleWiringSecretRead"]["Properties"][
            "PolicyDocument"
        ]["Statement"][0]["Resource"].append(SECRET_PREFIX + "database-url-fyOYuj"),
        lambda t: t["Resources"]["ExecutionRoleWiringSecretRead"]["Properties"][
            "PolicyDocument"
        ]["Statement"][0].__setitem__("Action", "secretsmanager:*"),
        lambda t: t["Resources"]["ExecutionRoleWiringSecretRead"][
            "Properties"
        ].__setitem__("Roles", [TASK_ROLE]),
        lambda t: t["Resources"]["BillingObservabilitySmokeTokenSecret"][
            "Properties"
        ].__setitem__("SecretString", CANARY_TEXT),
        lambda t: t["Resources"]["BillingObservabilitySmokeTokenSecret"][
            "Properties"
        ].__setitem__("KmsKeyId", "alias/aws/secretsmanager"),
        lambda t: t["Resources"]["BillingObservabilitySmokeTokenSecret"].__setitem__(
            "DeletionPolicy", "Delete"
        ),
        lambda t: t["Parameters"]["AsaasApiKeySecretArn"].__setitem__(
            "AllowedPattern", "^arn:aws:secretsmanager:.*$"
        ),
        lambda t: t["Outputs"]["BillingObservabilitySmokeTokenSecretArn"].__setitem__(
            "Value", "{{resolve:secretsmanager:x}}"
        ),
    ],
)
def test_template_validator_rejects_any_widening(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    template = wiring.load_yaml(TEMPLATE)
    mutate(template)
    with pytest.raises(wiring.WiringError):
        wiring.validate_secret_access_template(template)


def test_plan_creates_the_stack_with_the_two_resolved_arns_only() -> None:
    plan, rendered = _plan(FakeAws())
    step = _step(plan, "secret_access_stack")
    assert step["state"] == "would_change"
    (change,) = step["changes"]
    assert change["action"] == "create_cloudformation_stack"
    assert change["parameters"] == {
        "AsaasApiKeySecretArn": API_KEY_ARN,
        "AsaasWebhookTokenSecretArn": WEBHOOK_TOKEN_ARN,
    }
    assert change["capabilities"] == ["CAPABILITY_IAM"]
    document = change["creates"][1]["document"]
    assert document["Statement"][0]["Resource"][:2] == [API_KEY_ARN, WEBHOOK_TOKEN_ARN]
    assert len(document["Statement"][0]["Resource"]) == 3
    assert rendered is None
    assert _step(plan, "task_definition_revision")["state"] == "pending_dependency"
    assert plan["status"] == "ready"


def test_wiring_policy_check_accepts_exactly_the_three_arns() -> None:
    expected = {API_KEY_ARN, WEBHOOK_TOKEN_ARN, SMOKE_ARN}
    exact = wiring.expected_wiring_policy([API_KEY_ARN, WEBHOOK_TOKEN_ARN, SMOKE_ARN])
    assert wiring.wiring_policy_problems(exact, expected) == []
    extra = copy.deepcopy(exact)
    extra["Statement"][0]["Resource"].append(SECRET_PREFIX + "database-url-fyOYuj")
    assert wiring.wiring_policy_problems(extra, expected) == ["policy_resources"]
    missing = copy.deepcopy(exact)
    missing["Statement"][0]["Resource"].pop()
    assert wiring.wiring_policy_problems(missing, expected) == ["policy_resources"]
    wildcard = copy.deepcopy(exact)
    wildcard["Statement"][0]["Resource"] = [SECRET_PREFIX + "asaas-*"]
    assert wiring.wiring_policy_problems(wildcard, expected) == [
        "policy_wildcard_resource"
    ]
    action = copy.deepcopy(exact)
    action["Statement"][0]["Action"] = ["secretsmanager:GetSecretValue", "kms:Decrypt"]
    assert wiring.wiring_policy_problems(action, expected) == ["policy_action"]
    second = copy.deepcopy(exact)
    second["Statement"].append(copy.deepcopy(second["Statement"][0]))
    assert wiring.wiring_policy_problems(second, expected) == ["policy_statement_count"]


def test_applied_stack_is_compliant_and_task_definition_would_register() -> None:
    plan, rendered = _plan(FakeAws(applied=True))
    assert _step(plan, "secret_access_stack")["state"] == "compliant"
    assert "wiring_policy_exact" in _step(plan, "secret_access_stack")["compliant"]
    step = _step(plan, "task_definition_revision")
    assert step["state"] == "would_change"
    assert step["changes"][0]["action"] == "register_task_definition"
    assert step["changes"][0]["derived_from"] == SOURCE_ARN
    assert rendered is not None
    assert plan["status"] == "ready"


def test_wiring_policy_drift_blocks_the_plan() -> None:
    fake = FakeAws(applied=True)
    fake.apply_stack(
        policy_arns=[
            API_KEY_ARN,
            WEBHOOK_TOKEN_ARN,
            SMOKE_ARN,
            SECRET_PREFIX + "other-x1y2z3",
        ]
    )
    plan, _rendered = _plan(fake)
    assert plan["status"] == "blocked"
    assert (
        "wiring_policy_drift:policy_resources"
        in _step(plan, "secret_access_stack")["blocks"]
    )


def test_smoke_token_created_outside_the_stack_blocks() -> None:
    fake = FakeAws()
    fake.put(
        "secretsmanager",
        "DescribeSecret",
        {"SecretId": "neurostack/staging/billing-observability-smoke-token"},
        _secret("billing-observability-smoke-token", SMOKE_ARN),
    )
    plan, _rendered = _plan(fake)
    assert (
        "smoke_token_secret_exists_outside_stack"
        in _step(plan, "secret_access_stack")["blocks"]
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            {"KmsKeyId": "arn:aws:kms:us-east-1:388460142431:key/other"},
            "kms_key_mismatch",
        ),
        ({"DeletedDate": "2026-10-18T00:00:00Z"}, "scheduled_for_deletion"),
        ({"VersionIdsToStages": {"v1": ["AWSPENDING"]}}, "no_current_version"),
        ({"ARN": SECRET_PREFIX + "asaas-api-key-AbC12"}, "arn_unexpected"),
    ],
)
def test_existing_secret_problems_block(change: dict[str, Any], reason: str) -> None:
    fake = FakeAws()
    secret = _secret("asaas-api-key", API_KEY_ARN)
    secret.update(change)
    fake.put(
        "secretsmanager",
        "DescribeSecret",
        {"SecretId": "neurostack/staging/asaas-api-key"},
        secret,
    )
    plan, _rendered = _plan(fake)
    assert any(
        reason in block for block in _step(plan, "secret_access_stack")["blocks"]
    )


def test_missing_kms_decrypt_grant_blocks() -> None:
    fake = FakeAws()
    document = copy.deepcopy(TERRAFORM_POLICY)
    document["Statement"].pop()
    fake.put(
        "iam",
        "GetRolePolicy",
        {"RoleName": EXECUTION_ROLE, "PolicyName": "segredos-e-logs"},
        _policy(document, "segredos-e-logs"),
    )
    plan, _rendered = _plan(fake)
    assert (
        "kms_decrypt_not_granted_for_secret_key"
        in _step(plan, "secret_access_stack")["blocks"]
    )


def test_task_role_needs_no_change() -> None:
    plan, _rendered = _plan(FakeAws())
    info = _step(plan, "secret_access_stack")["info"]["task_role_change"]
    assert info["required"] is False


# ---------------------------------------------------------------------------
# Task definition rendering and evidence validators
# ---------------------------------------------------------------------------


def test_rendering_keeps_app_and_otel_and_adds_exactly_the_d3_names() -> None:
    _plan_result, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    containers = evidence._container_map(rendered)
    assert set(containers) == {"app", "otel"}
    assert evidence._env_map(containers["otel"]) == {}
    assert evidence._secret_map(containers["otel"]) == {}
    source = _task_definition()["taskDefinition"]["containerDefinitions"]
    # otel changes only its image: the manifest's digest pin of the live tag.
    assert containers["otel"] == {**source[1], "image": OTEL_DIGEST_IMAGE}
    app_env = evidence._env_map(containers["app"])
    app_secrets = evidence._secret_map(containers["app"])
    assert set(app_env) == evidence._BASE_ENV_NAMES | set(wiring.ADDED_ENVIRONMENT)
    assert set(app_secrets) == evidence._EXPECTED_APP_SECRET_NAMES | set(
        wiring.ADDED_SECRETS
    )
    # The reviewed parameter, rendered as is; its repository value is pinned
    # once, by test_the_repository_turns_the_outbox_worker_on.
    assert app_env["BILLING_FACT_WORKER_ENABLED"] == (
        _validated()["BILLING_FACT_WORKER_ENABLED"]
    )
    assert app_env["ASAAS_WEBHOOK_URL"] == (
        "https://staging.api.neurostack.global/v1/billing/webhook/asaas"
    )
    assert app_env["ASAAS_CHECKOUT_RETURN_URL"] == (
        "https://payments.staging.api.neurostack.global/billing"
    )
    assert app_env["ASAAS_WEBHOOK_NOTIFICATION_EMAIL"] == "neurostack.infra@gmail.com"
    assert {
        key: value
        for key, value in rendered.items()
        if key not in {"containerDefinitions", "tags"}
    } == {
        key: value
        for key, value in wiring._request_from_response(_task_definition()).items()
        if key not in {"containerDefinitions", "tags"}
    }
    # Tags: the source tags plus ManagedBy=manual (owner approval A4).
    assert rendered["tags"] == [
        {"key": "Project", "value": "neurostack-api"},
        {"key": "Environment", "value": "staging"},
        {"key": "ManagedBy", "value": "manual"},
    ]
    assert not set(rendered) & evidence._RESPONSE_ONLY_FIELDS


def test_secrets_appear_only_as_valuefrom_references() -> None:
    _plan_result, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    app = evidence._container_map(rendered)["app"]
    secrets = evidence._secret_map(app)
    assert secrets["ASAAS_API_KEY"] == API_KEY_ARN
    assert secrets["ASAAS_WEBHOOK_TOKEN"] == WEBHOOK_TOKEN_ARN
    assert secrets["BILLING_OBSERVABILITY_SMOKE_TOKEN"] == SMOKE_ARN
    environment = evidence._env_map(app)
    assert not set(wiring.ADDED_SECRETS) & set(environment)
    assert not any("secretsmanager" in value for value in environment.values())
    assert all(set(row) == {"name", "valueFrom"} for row in app["secrets"])


def test_structural_evidence_validators_and_group5_template_accept_the_rendering() -> (
    None
):
    _plan_result, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    results = wiring.run_evidence_validators(rendered, SOURCE_ARN)
    structural = [row for row in results if row["scope"] == "structural"]
    assert structural and all(row["result"] == "pass" for row in structural)
    (deploy_path,) = [row for row in results if row["scope"] == "group5_deploy_path"]
    # F1 path A: the protected template validator accepts exactly the D3 names.
    assert deploy_path == {
        "validator": "asaas_producer_rollout_evidence.validate_task_definition_template",
        "scope": "group5_deploy_path",
        "result": "pass",
    }
    # It still fails closed on any name outside the reviewed sets.
    extra = copy.deepcopy(rendered)
    evidence._container_map(extra)["app"]["environment"].append(
        {"name": "ASAAS_SANDBOX_API_URL", "value": "https://sandbox.example"}
    )
    assert wiring.group5_template_verdict(extra, SOURCE_ARN) == (
        False,
        "task_definition_base_environment_names_mismatch",
    )


def test_validator_d3_names_are_exactly_the_manifest_and_planner_names() -> None:
    manifest = wiring.load_yaml(MANIFEST)["task_definition"]
    assert rollout.D3_ENVIRONMENT_NAMES == set(wiring.ADDED_ENVIRONMENT)
    assert rollout.D3_ENVIRONMENT_NAMES == set(manifest["app_environment_added"])
    assert rollout.D3_SECRET_NAMES == set(wiring.ADDED_SECRETS)
    assert rollout.D3_SECRET_NAMES == set(manifest["app_secrets_added"])
    assert rollout.D3_SECRET_NAMES == {row[0] for row in wiring.SECRET_REFERENCES}
    assert not rollout.D3_ENVIRONMENT_NAMES & rollout.D3_SECRET_NAMES
    assert rollout.SOURCE_IMAGE_REPOSITORY == LIVE_IMAGE.split(":", 1)[0]


def _registered_revision_9(rendered: dict[str, Any]) -> dict[str, Any]:
    """DescribeTaskDefinition of revision 9 as registered from the rendering."""

    registered = _task_definition(NEXT_ARN)
    registered["taskDefinition"]["containerDefinitions"] = copy.deepcopy(
        rendered["containerDefinitions"]
    )
    registered["tags"] = copy.deepcopy(rendered["tags"])
    return registered


PINNED_DATABASE_REFERENCE = (
    SECRET_PREFIX + "database-url-fyOYuj:::12345678-1234-5678-1234-567812345678"
)


def _protected_template_from_revision_9(response: dict[str, Any]) -> dict[str, Any]:
    """The documented regeneration of ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON."""

    return {
        "schema_version": evidence.TASK_DEFINITION_TEMPLATE_SCHEMA,
        "source_task_definition_arn": response["taskDefinition"]["taskDefinitionArn"],
        "task_definition": wiring._request_from_response(response),
    }


def test_template_regenerated_from_revision_9_validates_clean() -> None:
    plan, rendered = _plan(FakeAws(applied=True), app_image_override=OVERRIDE_IMAGE)
    assert rendered is not None
    template = _protected_template_from_revision_9(_registered_revision_9(rendered))
    assert template["source_task_definition_arn"] == NEXT_ARN
    with wiring._synthetic_trust_pins():
        validated = evidence.validate_task_definition_template(template)
        request = evidence.build_replacement_task_definition(
            template,
            image_uri=wiring.PRODUCER_IMAGE_REPOSITORY,
            app_digest="sha256:" + "a" * 64,
            pinned_database_value_from=PINNED_DATABASE_REFERENCE,
        )
    assert set(validated["base_environment_sha256"]) == (
        evidence._BASE_ENV_NAMES | set(wiring.ADDED_ENVIRONMENT)
    )
    assert validated["app_secret_names"] == sorted(
        evidence._EXPECTED_APP_SECRET_NAMES | set(wiring.ADDED_SECRETS)
    )
    app = evidence._container_map(request)["app"]
    assert app["image"] == f"{wiring.PRODUCER_IMAGE_REPOSITORY}@sha256:" + "a" * 64
    assert (
        rollout.classify_app_image(
            OVERRIDE_IMAGE, target_repository=wiring.PRODUCER_IMAGE_REPOSITORY
        )
        == "source"
    )
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert not any(reason.startswith("F1_group5") for reason in reasons)
    assert "F1_protected_path_image_repository_mismatch" not in reasons


def test_template_regenerated_from_a_tagged_revision_9_is_refused_by_the_deploy() -> (
    None
):
    """Without app_image_override revision 9 keeps revision 8's mutable tag."""

    _plan_result, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    template = _protected_template_from_revision_9(_registered_revision_9(rendered))
    with wiring._synthetic_trust_pins():
        evidence.validate_task_definition_template(template)
        with pytest.raises(
            evidence.EvidenceError,
            match="^source_image_repository_mismatch:"
            "source_image_reference_not_digest_pinned$",
        ):
            evidence.build_replacement_task_definition(
                template,
                image_uri=wiring.PRODUCER_IMAGE_REPOSITORY,
                app_digest="sha256:" + "a" * 64,
                pinned_database_value_from=PINNED_DATABASE_REFERENCE,
            )


def test_rollback_target_itself_passes_the_group5_template_validator() -> None:
    request = wiring._request_from_response(_task_definition())
    assert wiring.group5_template_verdict(request, SOURCE_ARN) == (True, "pass")


def test_synthetic_trust_pins_are_scoped_and_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BILLING_PRODUCER_GRANT_KMS_KEY_ARN", "operator-value")
    monkeypatch.delenv("BILLING_PRODUCER_BUILD_KMS_KEY_ARN", raising=False)
    with wiring._synthetic_trust_pins():
        assert os.environ["BILLING_PRODUCER_GRANT_KMS_KEY_ARN"] != "operator-value"
        assert "BILLING_PRODUCER_BUILD_KMS_KEY_ARN" in os.environ
    assert os.environ["BILLING_PRODUCER_GRANT_KMS_KEY_ARN"] == "operator-value"
    assert "BILLING_PRODUCER_BUILD_KMS_KEY_ARN" not in os.environ


def test_image_reference_is_preserved_from_the_live_revision() -> None:
    plan, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    assert evidence._container_map(rendered)["app"]["image"] == LIVE_IMAGE
    assert plan["task_definition_diff"]["app_image_changed"] is False
    assert plan["task_definition_diff"]["live_app_image_digest"] == LIVE_DIGEST


def test_image_override_must_be_a_digest_that_exists_in_ecr() -> None:
    plan, rendered = _plan(FakeAws(applied=True), app_image_override=OVERRIDE_IMAGE)
    assert rendered is not None
    assert evidence._container_map(rendered)["app"]["image"] == OVERRIDE_IMAGE
    assert plan["task_definition_diff"]["app_image_changed"] is True
    fake = FakeAws(applied=True)
    fake.put(
        "ecr",
        "DescribeImages",
        {
            "repositoryName": "neurostack-staging",
            "imageIds": [{"imageDigest": "sha256:" + "2" * 64}],
        },
        {"__error_code__": "ImageNotFoundException"},
    )
    plan, _rendered = _plan(fake, app_image_override=OVERRIDE_IMAGE)
    assert (
        "app_image_override_not_found_in_ecr"
        in _step(plan, "task_definition_revision")["blocks"]
    )
    for invalid in (
        LIVE_IMAGE,
        "docker.io/library/python@sha256:" + "2" * 64,
        OVERRIDE_IMAGE.replace("sha256:", "sha256:A"),
    ):
        with pytest.raises(wiring.WiringError, match="parameter_invalid:app_image"):
            _validated(app_image_override=invalid)


def test_initial_d3_world_rollback_target_was_revision_8() -> None:
    """The manifest of the D3 world read 8 until its documented re-pin to 9."""

    plan, _rendered = _plan(FakeAws(applied=True))
    assert plan["target"]["rollback_target"] == SOURCE_ARN
    assert SOURCE_ARN.endswith(":task-definition/neurostack-staging-app:8")
    assert _step(plan, "service_deployment")["info"]["rollback_target"] == SOURCE_ARN
    assert plan["task_definition_diff"]["source_task_definition_arn"] == SOURCE_ARN


def test_registered_revision_matching_the_rendering_is_compliant() -> None:
    fake = FakeAws(applied=True)
    _plan_result, rendered = _plan(fake)
    assert rendered is not None
    registered = _registered_revision_9(rendered)
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app", "include": ["TAGS"]},
        registered,
    )
    plan, _rendered = _plan(fake)
    assert (
        f"registered_not_deployed:{NEXT_ARN}"
        in _step(plan, "task_definition_revision")["compliant"]
    )
    assert plan["status"] == "compliant"
    fake.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(NEXT_ARN),
    )
    plan, _rendered = _plan(fake)
    assert (
        f"registered_and_deployed:{NEXT_ARN}"
        in _step(plan, "task_definition_revision")["compliant"]
    )


def test_unexpected_newer_revision_or_service_drift_blocks() -> None:
    fake = FakeAws(applied=True)
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app", "include": ["TAGS"]},
        _task_definition(NEXT_ARN),
    )
    plan, _rendered = _plan(fake)
    assert (
        f"newer_revision_differs_from_rendering:{NEXT_ARN}"
        in _step(plan, "task_definition_revision")["blocks"]
    )
    fake.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(NEXT_ARN),
    )
    plan, _rendered = _plan(fake)
    assert (
        f"service_not_on_rollback_target:{NEXT_ARN}"
        in _step(plan, "task_definition_revision")["blocks"]
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"runningCount": 1}, "service_not_steady"),
        (
            {
                "deployments": [
                    {
                        "id": "a",
                        "status": "PRIMARY",
                        "taskDefinition": SOURCE_ARN,
                        "rolloutState": "IN_PROGRESS",
                    }
                ]
            },
            "service_rollout_not_complete",
        ),
        (
            {
                "deployments": [
                    {
                        "id": "a",
                        "status": "PRIMARY",
                        "taskDefinition": SOURCE_ARN,
                        "rolloutState": "COMPLETED",
                    },
                    {"id": "b", "status": "ACTIVE", "taskDefinition": NEXT_ARN},
                ]
            },
            "service_has_concurrent_deployments",
        ),
    ],
)
def test_unsteady_service_blocks(changes: dict[str, Any], reason: str) -> None:
    fake = FakeAws(applied=True)
    fake.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(**changes),
    )
    plan, _rendered = _plan(fake)
    assert reason in _step(plan, "task_definition_revision")["blocks"]


def test_deregistered_rollback_target_blocks() -> None:
    fake = FakeAws(applied=True)
    source = _task_definition()
    source["taskDefinition"]["status"] = "INACTIVE"
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:8", "include": ["TAGS"]},
        source,
    )
    plan, _rendered = _plan(fake)
    assert (
        "rollback_target_not_active"
        in _step(plan, "task_definition_revision")["blocks"]
    )


@pytest.mark.parametrize(
    "name", ["ABACATEPAY_WEBHOOK_SECRET", "APPCONFIG_ENABLED", "SPA_DIST_DIR"]
)
def test_forbidden_names_in_the_rollback_target_block(name: str) -> None:
    fake = FakeAws(applied=True)
    environment = _task_definition()["taskDefinition"]["containerDefinitions"][0][
        "environment"
    ] + [{"name": name, "value": "x"}]
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:8", "include": ["TAGS"]},
        _task_definition(app_environment=environment),
    )
    plan, rendered = _plan(fake)
    assert rendered is None
    blocks = _step(plan, "task_definition_revision")["blocks"]
    assert f"rollback_target_has_forbidden_names:{name}" in blocks
    if name.startswith("APPCONFIG_"):
        assert (
            "appconfig_names_in_rollback_target"
            in _step(plan, "financial_egress_closed")["blocks"]
        )


def test_financial_egress_stays_closed_by_absence() -> None:
    plan, rendered = _plan(FakeAws(applied=True))
    step = _step(plan, "financial_egress_closed")
    assert step["state"] == "compliant"
    assert step["info"]["financial_egress_enabled"] == "false (closed by absence)"
    assert rendered is not None
    names = json.dumps(rendered)
    assert "APPCONFIG" not in names


def test_no_abacatepay_name_anywhere() -> None:
    plan, rendered = _plan(FakeAws(applied=True))
    for text in (
        json.dumps(plan),
        json.dumps(rendered),
        TEMPLATE.read_text(encoding="utf-8"),
        PARAMS.read_text(encoding="utf-8"),
    ):
        assert "ABACATEPAY" not in text.upper()
    manifest_text = json.dumps(wiring.load_yaml(MANIFEST))
    assert manifest_text.count("ABACATEPAY") == 1
    assert wiring.load_yaml(MANIFEST)["task_definition"]["forbidden_name_prefixes"][
        0
    ] == ("ABACATEPAY_")


@pytest.mark.parametrize(
    "missing_host,reason",
    [
        ("payments.staging.api.neurostack.global", "ASAAS_CHECKOUT_RETURN_URL"),
        ("staging.api.neurostack.global", "ASAAS_WEBHOOK_URL"),
    ],
)
def test_url_host_must_resolve_to_the_load_balancer(
    missing_host: str, reason: str
) -> None:
    fake = FakeAws(applied=True)
    fake.put(
        "route53",
        "ListResourceRecordSets",
        {"HostedZoneId": ZONE_ID},
        {
            "ResourceRecordSets": [
                {"Name": f"{host}.", "Type": "A", "AliasTarget": {"DNSName": ALB_DNS}}
                for host in wiring.SERVED_HOSTS
                if host != missing_host
            ]
        },
    )
    plan, _rendered = _plan(fake)
    assert (
        f"host_not_served:{reason}:dns_alias_not_on_load_balancer"
        in _step(plan, "task_definition_revision")["blocks"]
    )


def test_certificate_must_cover_the_hosts() -> None:
    fake = FakeAws(applied=True)
    fake.put(
        "acm",
        "DescribeCertificate",
        {"CertificateArn": CERT_ARN},
        {
            "Certificate": {
                "Status": "ISSUED",
                "SubjectAlternativeNames": ["staging.api.neurostack.global"],
            }
        },
    )
    plan, _rendered = _plan(fake)
    assert (
        "host_not_served:ASAAS_CHECKOUT_RETURN_URL:certificate_does_not_cover_host"
        in _step(plan, "task_definition_revision")["blocks"]
    )


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


def test_reviewed_parameters_pass_the_application_settings_validators() -> None:
    values = yaml.safe_load(PARAMS.read_text(encoding="utf-8"))["parameters"]
    assert values == {
        "ASAAS_CHECKOUT_RETURN_URL": "https://payments.staging.api.neurostack.global/billing",
        "ASAAS_WEBHOOK_URL": "https://staging.api.neurostack.global/v1/billing/webhook/asaas",
        "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": "neurostack.infra@gmail.com",
        # Present and valid here; its repository value is pinned once, by
        # test_the_repository_turns_the_outbox_worker_on.
        "BILLING_FACT_WORKER_ENABLED": values["BILLING_FACT_WORKER_ENABLED"],
        "app_image_override": None,
    }
    assert (
        Settings.validate_asaas_checkout_return_url(values["ASAAS_CHECKOUT_RETURN_URL"])
        == values["ASAAS_CHECKOUT_RETURN_URL"]
    )
    assert (
        Settings.validate_asaas_webhook_url(values["ASAAS_WEBHOOK_URL"])
        == values["ASAAS_WEBHOOK_URL"]
    )
    assert wiring.validate_parameters(_parameters()) == values


def test_webhook_path_matches_the_application_receiver() -> None:
    from src.api.middleware.auth import AuthMiddleware
    from src.services.billing_webhook import ASAAS_WEBHOOK_PATH

    assert wiring.WEBHOOK_PATH == ASAAS_WEBHOOK_PATH
    middleware = AuthMiddleware(app=None)  # type: ignore[arg-type]
    assert middleware._is_public_path(ASAAS_WEBHOOK_PATH)
    assert not middleware._is_public_path(ASAAS_WEBHOOK_PATH + "/")
    assert not middleware._is_public_path("/v1/billing/summary")
    assert not middleware._is_public_path("/v1/billing/checkout")


@pytest.mark.parametrize("name", list(wiring.REQUIRED_PARAMETERS))
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_required_parameter_fails_the_plan(
    name: str,
    value: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(wiring.WiringError, match=f"parameter_missing:{name}"):
        _validated(**{name: value})
    raw = _parameters()
    del raw["parameters"][name]
    with pytest.raises(wiring.WiringError, match=f"parameter_missing:{name}"):
        wiring.validate_parameters(raw)
    snapshot = FakeAws().snapshot(tmp_path / "readback.json")
    params = _write_params(tmp_path, **{name: value})
    code, out, err = _run(
        ["plan", "--params", str(params), "--readback-file", str(snapshot)], capsys
    )
    assert code == 2
    assert out == ""
    assert f"blocked: parameter_missing:{name}" in err


@pytest.mark.parametrize(
    "value",
    [
        "http://staging.api.neurostack.global/v1/billing/webhook/asaas",
        "https://staging.api.neurostack.global/v1/billing/webhook/asaas/",
        "https://staging.api.neurostack.global/x/v1/billing/webhook/asaas",
        "https://staging.api.neurostack.global/v1/billing/webhook/asaas?x=1",
        "https://user:pw@staging.api.neurostack.global/v1/billing/webhook/asaas",
        "https://staging.api.neurostack.global:8443/v1/billing/webhook/asaas",
        "https://api.example.com/v1/billing/webhook/asaas",
        "https://STAGING.api.neurostack.global/v1/billing/webhook/asaas",
        "/v1/billing/webhook/asaas",
    ],
)
def test_webhook_url_must_be_the_exact_https_receiver(value: str) -> None:
    with pytest.raises(wiring.WiringError, match="parameter_invalid:ASAAS_WEBHOOK_URL"):
        _validated(ASAAS_WEBHOOK_URL=value)


@pytest.mark.parametrize(
    "value",
    [
        "http://payments.staging.api.neurostack.global/billing",
        "/billing",
        "payments.staging.api.neurostack.global/billing",
        "https://user@payments.staging.api.neurostack.global/billing",
        "https://payments.staging.api.neurostack.global/billing?x=1",
        "https://payments.staging.api.neurostack.global/billing#x",
        "https://payments.staging.api.neurostack.global/",
        "https://payments.staging.api.neurostack.global/billing/",
        "https://evil.example/billing",
    ],
)
def test_return_url_must_be_absolute_https_without_userinfo(value: str) -> None:
    with pytest.raises(
        wiring.WiringError, match="parameter_invalid:ASAAS_CHECKOUT_RETURN_URL"
    ):
        _validated(ASAAS_CHECKOUT_RETURN_URL=value)


def test_both_served_hosts_are_accepted_for_each_url() -> None:
    for host in wiring.SERVED_HOSTS:
        _validated(
            ASAAS_CHECKOUT_RETURN_URL=f"https://{host}/billing",
            ASAAS_WEBHOOK_URL=f"https://{host}/v1/billing/webhook/asaas",
        )


@pytest.mark.parametrize(
    "value",
    ["not-an-email", "a@b", "a b@neurostack.global", "ação@neurostack.global", "@x.io"],
)
def test_notification_email_is_validated(value: str) -> None:
    with pytest.raises(
        wiring.WiringError, match="parameter_invalid:ASAAS_WEBHOOK_NOTIFICATION_EMAIL"
    ):
        _validated(ASAAS_WEBHOOK_NOTIFICATION_EMAIL=value)


def test_unknown_parameter_is_rejected() -> None:
    raw = _parameters()
    raw["parameters"]["ABACATEPAY_WEBHOOK_SECRET"] = "x"
    with pytest.raises(wiring.WiringError, match="parameter_unknown"):
        wiring.validate_parameters(raw)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_and_template_validate() -> None:
    manifest, parameters, digests = wiring.load_inputs(MANIFEST, PARAMS)
    assert manifest["rollback_target"] == {
        "revision": 10,
        "effective_after": wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG,
    }
    assert "rollback_task_definition_revision" not in manifest["target"]
    assert parameters is not None
    assert set(digests) == {"manifest_sha256", "template_sha256", "parameters_sha256"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["secrets"]["references"].append(
            {
                "environment_name": "DATABASE_URL",
                "secret_name": "neurostack/staging/database-url",
                "source": "existing",
                "stack_parameter": "X",
            }
        ),
        lambda m: m["secrets"].__setitem__(
            "execution_role_policy_name", "segredos-e-logs"
        ),
        lambda m: m["task_definition"]["app_environment_added"].__setitem__(
            "APPCONFIG_ENABLED", {"value": "true"}
        ),
        lambda m: m["task_definition"]["app_environment_added"].__setitem__(
            "SPA_ENABLED", {"value": "true"}
        ),
        lambda m: m["financial_egress"].__setitem__("initial_state", "open"),
        lambda m: m["steps"][5].__setitem__("scope", "d3_apply"),
        lambda m: m["steps"].reverse(),
        lambda m: m["steps"][3].__setitem__("approval", "approved"),
        lambda m: m["rollback_target"].__setitem__("revision", 7),
        lambda m: m["target"].__setitem__("rollback_task_definition_revision", 8),
        lambda m: m["target"]["served_hosts"].append("evil.example"),
        lambda m: m["parameters"]["required"].pop("ASAAS_WEBHOOK_NOTIFICATION_EMAIL"),
        lambda m: m["known_blocks"].pop(),
        lambda m: m["context"].__setitem__("arch", "ABACATEPAY_ fallback"),
    ],
)
def test_manifest_validator_rejects_any_widening(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    mutate(manifest)
    with pytest.raises(wiring.WiringError):
        wiring.validate_manifest(manifest)


def test_yaml_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "dup.yaml"
    path.write_text("a: 1\na: 2\n", encoding="utf-8")
    with pytest.raises(wiring.WiringError, match="yaml_duplicate_key"):
        wiring.load_yaml(path)


# ---------------------------------------------------------------------------
# CLI and output hygiene
# ---------------------------------------------------------------------------


def test_cli_plan_render_and_check_from_a_readback_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws(applied=True).snapshot(tmp_path / "readback.json")
    args = [
        "--params",
        str(PARAMS),
        "--readback-file",
        str(snapshot),
        "--manifest",
        str(_initial_manifest_file(tmp_path)),
    ]
    code, out, err = _run(["plan", *args], capsys)
    assert code == 0, err
    plan = json.loads(out)
    assert plan["status"] == "ready"
    assert plan["readback_source"] == "snapshot"
    assert set(plan["aws_operations"]) <= {
        f"{s}.{o}" for s, o in wiring.ALLOWED_OPERATIONS
    }
    code, out, err = _run(["render-taskdef", *args], capsys)
    assert code == 0, err
    rendered = json.loads(out)
    assert rendered["family"] == "neurostack-staging-app"
    assert plan["task_definition_diff"]["rendered_request_sha256"] == wiring._sha256(
        rendered
    )
    code, out, err = _run(["check", *args], capsys)
    assert code == 0, err
    check = json.loads(out)
    assert check["structural_status"] == "pass"
    assert check["group5_deploy_path_status"] == "pass"
    assert check["status"] == "pass"


def test_render_refuses_before_the_secret_access_stack_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws().snapshot(tmp_path / "readback.json")
    code, out, err = _run(
        [
            "render-taskdef",
            "--params",
            str(PARAMS),
            "--readback-file",
            str(snapshot),
            "--manifest",
            str(_initial_manifest_file(tmp_path)),
        ],
        capsys,
    )
    assert code == 2
    assert out == ""
    # The manifest records step 1 as applied, so a missing stack is a block.
    assert "blocked: plan_blocked:applied_record_but_stack_absent" in err
    parameters = _validated()
    readback = wiring.collect_readback(
        wiring.ReadOnlyAws(FakeAws().transport), parameters, pins=_initial_pins()
    )
    plan, rendered = wiring.build_plan(
        parameters,
        readback,
        {"template_sha256": "t" * 64},
        applied=None,
        pins=_initial_pins(),
    )
    assert plan["status"] == "ready"
    with pytest.raises(wiring.WiringError, match="secret_access_stack_not_compliant"):
        wiring.renderable(plan, rendered)


def test_producer_repository_pin_matches_the_release_preflight() -> None:
    from scripts import asaas_build_release_preflight as preflight

    assert wiring.PRODUCER_IMAGE_REPOSITORY == preflight.BOOTSTRAP_ECR_URI
    # The read-only tool never imports the HTTP-capable preflight module.
    source = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(
        r"^\s*(?:from|import)\s+\S*asaas_build_release_preflight", source, re.M
    )
    assert not re.search(
        r"^\s*from scripts import .*asaas_build_release_preflight", source, re.M
    )


def test_output_never_contains_secret_like_material(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeAws(applied=True)
    snapshot = fake.snapshot(tmp_path / "readback.json")
    args = ["--params", str(PARAMS), "--readback-file", str(snapshot)]
    outputs = []
    for command in ("plan", "render-taskdef", "check"):
        _code, out, err = _run([command, *args], capsys)
        outputs.append(out + err)
    preview_snapshot = FakeAws().snapshot(tmp_path / "preview.json")
    _code, out, err = _run(
        ["plan", "--params", str(PARAMS), "--readback-file", str(preview_snapshot)],
        capsys,
    )
    outputs.append(out + err)
    for text in outputs:
        assert CANARY_TEXT not in text
        assert "PREVEW" not in text
        assert wiring._SECRET_LIKE_OUTPUT.search(text) is None
    with pytest.raises(
        wiring.WiringError, match="output_contains_secret_like_material"
    ):
        wiring._emit({"x": CANARY_PEM})
    with pytest.raises(
        wiring.WiringError, match="output_contains_secret_like_material"
    ):
        wiring._emit({"x": CANARY_KEY})


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (
            lambda raw: raw["reads"][0]["response"].__setitem__(
                "SecretString", CANARY_KEY
            ),
            "secret_material_in_readback",
        ),
        (
            lambda raw: raw["reads"].append(
                {
                    "service": "secretsmanager",
                    "operation": "GetSecretValue",
                    "params": {},
                    "response": {},
                }
            ),
            "readback_file_operation_not_allowed",
        ),
        (
            lambda raw: raw.__setitem__("account_id", "111111111111"),
            "readback_file_shape_invalid",
        ),
        (
            lambda raw: raw["reads"].append(raw["reads"][0]),
            "readback_file_duplicate_read",
        ),
    ],
)
def test_readback_file_is_validated(
    mutate: Callable[[dict[str, Any]], None], reason: str, tmp_path: Path
) -> None:
    path = FakeAws().snapshot(tmp_path / "readback.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(wiring.WiringError, match=reason):
        wiring.snapshot_transport(path)


def test_readback_file_missing_a_read_fails_closed(tmp_path: Path) -> None:
    fake = FakeAws()
    del fake.responses[
        wiring._read_key(
            "iam",
            "GetRolePolicy",
            {"RoleName": EXECUTION_ROLE, "PolicyName": "segredos-e-logs"},
        )
    ]
    transport = wiring.snapshot_transport(fake.snapshot(tmp_path / "readback.json"))
    with pytest.raises(wiring.WiringError, match="readback_missing:iam.GetRolePolicy"):
        wiring.collect_readback(
            wiring.ReadOnlyAws(transport), _validated(), pins=_initial_pins()
        )


def test_wrong_account_blocks_everything() -> None:
    fake = FakeAws(applied=True)
    fake.put("sts", "GetCallerIdentity", {}, {"Account": "111111111111"})
    plan, _rendered = _plan(fake)
    assert plan["status"] == "blocked"
    assert {"step": "preflight", "reason": "aws_account_mismatch"} in plan["blocks"]


def test_gates_are_never_performed_and_report_their_known_blocks() -> None:
    plan, _rendered = _plan(FakeAws())
    for step in plan["steps"]:
        if step["scope"] == "separate_approval":
            assert step["changes"] == []
            assert step["state"] in {"not_performed", "blocked"}
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    # F1 path A: the deploy-path template validator accepts the D3 names; the
    # live image (a mutable tag outside the producer repository) still blocks.
    assert not any(reason.startswith("F1_group5") for reason in reasons)
    assert (
        "signed_image_build_and_attestation:"
        "group5_deploy_template_validator_accepts_rendering" in plan["compliant"]
    )
    assert "F1_protected_path_image_repository_mismatch" in reasons
    # F3 is a fact checked at plan time: reported only without the CLI file.
    assert ("F3_webhook_registration_entry_point:missing" in reasons) is (
        not (ROOT / "src/ops/asaas_webhook.py").is_file()
    )
    assert (
        _step(plan, "signed_image_build_and_attestation")["info"][
            "deploy_path_verdict_evaluated_on"
        ]
        == "preview"
    )


def test_deployment_safety_settings_and_waf_enforcement_are_gate_blocks() -> None:
    fake = FakeAws(applied=True)
    fake.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(
            deploymentConfiguration={
                "deploymentCircuitBreaker": {"enable": True, "rollback": False},
                "maximumPercent": 200,
                "minimumHealthyPercent": 100,
            }
        ),
    )
    fake.put(
        "wafv2",
        "GetWebACLForResource",
        {"ResourceArn": LB_ARN},
        {
            "WebACL": {
                "DefaultAction": {"Allow": {}},
                "Rules": [
                    {
                        "Name": "AWSManagedRulesCommonRuleSet",
                        "OverrideAction": {"None": {}},
                    }
                ],
            }
        },
    )
    fake.put(
        "elbv2",
        "DescribeRules",
        {"ListenerArn": HTTPS_LISTENER_ARN},
        {
            "Rules": [
                {
                    "Priority": "1",
                    "IsDefault": False,
                    "Conditions": [{"Field": "path-pattern"}],
                },
                {"Priority": "default", "IsDefault": True},
            ]
        },
    )
    plan, _rendered = _plan(fake)
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "deployment_safety_settings_changed" in reasons
    assert "waf_rule_may_block_receiver:AWSManagedRulesCommonRuleSet" in reasons
    assert "https_listener_rules_need_review" in reasons
    assert plan["status"] == "ready"


# ---------------------------------------------------------------------------
# Fix loop 1: rendering is refused whenever the plan is blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", sorted(BLOCKED_STATES))
def test_render_and_check_refuse_whenever_the_plan_is_blocked(
    state: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reach, reason = BLOCKED_STATES[state]
    fake = FakeAws()
    reach(fake)
    plan, _rendered = _plan(fake)
    assert plan["status"] == "blocked"
    assert any(block["reason"].startswith(reason) for block in plan["blocks"]), plan[
        "blocks"
    ]
    snapshot = fake.snapshot(tmp_path / "readback.json")
    args = [
        "--params",
        str(PARAMS),
        "--readback-file",
        str(snapshot),
        "--manifest",
        str(_initial_manifest_file(tmp_path)),
    ]
    code, out, _err = _run(["plan", *args], capsys)
    assert code == 2
    assert json.loads(out)["status"] == "blocked"
    for command in ("render-taskdef", "check"):
        code, out, err = _run([command, *args], capsys)
        assert code == 2, command
        assert out == "", command
        assert "blocked: plan_blocked:" in err, command


def test_render_requires_the_secret_access_stack_to_be_compliant() -> None:
    fake = FakeAws(applied=True)
    plan, rendered = _plan(fake)
    assert _step(plan, "secret_access_stack")["state"] == "compliant"
    assert rendered is not None
    assert wiring.renderable(plan, rendered) is rendered
    stale = copy.deepcopy(plan)
    stale["steps"][0]["state"] = "would_change"
    with pytest.raises(wiring.WiringError, match="secret_access_stack_not_compliant"):
        wiring.renderable(stale, rendered)


# ---------------------------------------------------------------------------
# Fix loop 1: the applied stack is checked as a whole
# ---------------------------------------------------------------------------


def test_applied_stack_resources_and_template_are_verified() -> None:
    plan, _rendered = _plan(FakeAws(applied=True))
    compliant = _step(plan, "secret_access_stack")["compliant"]
    assert "stack_resources_exact" in compliant
    assert "stack_template_matches_reviewed_template" in compliant
    assert "applied_record_matches_live_state" in compliant


def test_stack_secret_resource_must_be_the_live_smoke_token() -> None:
    resources = copy.deepcopy(STACK_RESOURCES)
    resources[0]["PhysicalResourceId"] = SECRET_PREFIX + "other-AbC123"
    fake = FakeAws()
    fake.apply_stack(resources=resources)
    plan, rendered = _plan(fake)
    assert "stack_resources_mismatch" in _step(plan, "secret_access_stack")["blocks"]
    assert plan["status"] == "blocked"


@pytest.mark.parametrize(
    "field,value,reason",
    [
        (
            "smoke_token_secret_arn",
            SECRET_PREFIX + "billing-observability-smoke-token-AbC123",
            "applied_record_smoke_token_arn_mismatch",
        ),
        ("change_set_name", "another-change-set", "applied_record_change_set_mismatch"),
    ],
)
def test_applied_record_must_match_live_state(
    field: str, value: str, reason: str
) -> None:
    applied = dict(APPLIED, **{field: value})
    plan, _rendered = _plan(FakeAws(applied=True), applied=applied)
    assert reason in _step(plan, "secret_access_stack")["blocks"]


def test_manifest_records_step_one_as_applied() -> None:
    manifest = wiring.validate_manifest(wiring.load_yaml(MANIFEST))
    steps = {step["id"]: step for step in manifest["steps"]}
    assert steps["secret_access_stack"]["applied"] == APPLIED
    assert all(
        "applied" not in step
        for key, step in steps.items()
        if key != "secret_access_stack"
    )
    assert wiring.applied_record(manifest) == APPLIED


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["steps"][0]["applied"].__setitem__(
            "smoke_token_secret_arn", SECRET_PREFIX + "asaas-api-key-PFqO2v"
        ),
        lambda m: m["steps"][0]["applied"].__setitem__("execution_options", "none"),
        lambda m: m["steps"][0]["applied"].__setitem__("stack_name", "other"),
        lambda m: m["steps"][0]["applied"].__setitem__("secret_value", "x"),
        lambda m: m["steps"][0].pop("applied"),
        lambda m: m["steps"][1].__setitem__("applied", copy.deepcopy(APPLIED)),
    ],
)
def test_manifest_applied_record_is_pinned(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    mutate(manifest)
    with pytest.raises(wiring.WiringError):
        wiring.validate_manifest(manifest)


# ---------------------------------------------------------------------------
# Migrations window 033-035: exact revision, never ``upgrade head``
# ---------------------------------------------------------------------------

UPGRADE_HEAD = re.compile(r"upgrade\s+heads?\b", re.IGNORECASE)


def _migrations_step(manifest: dict[str, Any]) -> dict[str, Any]:
    return next(row for row in manifest["steps"] if row["id"] == "migrations_033_035")


def test_migration_windows_pin_each_exact_upgrade() -> None:
    """One table entry per window; the planner uses the window module's table."""

    assert wiring.MIGRATION_WINDOWS is mw.WINDOWS
    assert {name: (w.before, w.after, w.command) for name, w in mw.WINDOWS.items()} == {
        "035": ("032", "035", ("alembic", "upgrade", "035")),
        "036": ("035", "036", ("alembic", "upgrade", "036")),
        "037": ("036", "037", ("alembic", "upgrade", "037")),
    }
    assert mw.WINDOW_035.verification == "read back alembic_version == 035"
    assert mw.WINDOW_036.verification == "read back alembic_version == 036"
    assert mw.WINDOW_037.verification == "read back alembic_version == 037"
    assert set(wiring.MIGRATIONS_EXCLUDED) == set(mw.WINDOWS)
    window_steps = [w.manifest_step for w in mw.WINDOWS.values()]
    assert window_steps == ["migrations_033_035", "migrations_036", "migrations_037"]
    assert [s for s, _scope in wiring.STEPS if s.startswith("migrations")] == (
        window_steps
    )
    assert all(
        scope == "separate_approval"
        for step, scope in wiring.STEPS
        if step in window_steps
    )
    for legacy in (
        "MIGRATIONS_STEP",
        "MIGRATIONS_SOURCE_REVISION",
        "MIGRATIONS_TARGET_REVISION",
        "MIGRATIONS_COMMAND",
        "MIGRATIONS_VERIFICATION",
    ):
        assert not hasattr(wiring, legacy), legacy


def test_manifest_migrations_step_upgrades_to_exactly_035() -> None:
    manifest = wiring.validate_manifest(wiring.load_yaml(MANIFEST))
    step = _migrations_step(manifest)
    assert "`alembic upgrade 035`" in step["change"]
    assert any("alembic_version == 032" in row for row in step["preconditions"])
    assert any("alembic_version == 035" in row for row in step["verification"])
    assert UPGRADE_HEAD.search(json.dumps(manifest)) is None
    assert UPGRADE_HEAD.search(MANIFEST.read_text(encoding="utf-8")) is None


def _set_change(text: str) -> Callable[[dict[str, Any]], None]:
    return lambda m: _migrations_step(m).__setitem__("change", text)


def _set_verification(rows: list[str]) -> Callable[[dict[str, Any]], None]:
    return lambda m: _migrations_step(m).__setitem__("verification", rows)


@pytest.mark.parametrize(
    "mutate",
    [
        _set_change("one-off migrations task (alembic upgrade head)"),
        _set_change("one-off migrations task (`alembic upgrade heads`)"),
        _set_change("one-off migrations task (`alembic upgrade +3`)"),
        _set_change("one-off migrations task (`alembic upgrade 036`)"),
        _set_change("one-off migrations task (`alembic upgrade 0350`)"),
        _set_change("one-off task `alembic upgrade 035`, then `alembic upgrade 037`"),
        _set_change("one-off migrations task (alembic upgrade 035 without backticks)"),
        _set_verification(["read back alembic_version == 0350"]),
        _set_verification(
            ["read back alembic_version == 035", "then alembic_version == 037"]
        ),
        _set_change(
            "one-off migrations task (`alembic upgrade 035`), then alembic upgrade head"
        ),
        _set_verification(["Alembic head reads 035."]),
        _set_verification(["read back alembic_version == 037"]),
        lambda m: _migrations_step(m).__setitem__(
            "preconditions", ["Expand-only window."]
        ),
        lambda m: m["steps"][5].__setitem__(
            "change", m["steps"][5]["change"] + " Then run alembic upgrade head."
        ),
        lambda m: m["context"]["must"].append("alembic  upgrade   HEAD after deploy"),
        lambda m: m["context"]["must"].append("then run `upgrade head` in the task"),
        lambda m: m["steps"][5].__setitem__(
            "change", m["steps"][5]["change"] + " Then run `alembic upgrade 037`."
        ),
        _set_verification(["alembic_version == 035 eventually"]),
    ],
)
def test_manifest_validator_rejects_an_unpinned_migrations_window(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    mutate(manifest)
    with pytest.raises(wiring.WiringError, match="manifest_migrations_window"):
        wiring.validate_manifest(manifest)


def test_plan_prints_the_pinned_migrations_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws(applied=True).snapshot(tmp_path / "readback.json")
    code, out, err = _run(
        [
            "plan",
            "--params",
            str(PARAMS),
            "--readback-file",
            str(snapshot),
            "--manifest",
            str(_initial_manifest_file(tmp_path)),
        ],
        capsys,
    )
    assert code == 0, err
    info = _step(json.loads(out), "migrations_033_035")["info"]
    assert info["command"] == "alembic upgrade 035"
    assert info["target_revision"] == "035"
    assert info["required_current_revision"] == "032"
    assert info["verification"] == "read back alembic_version == 035"
    assert "036" in info["excluded_revisions"]
    assert "037" in info["excluded_revisions"]
    assert UPGRADE_HEAD.search(out) is None
    assert "alembic upgrade 035" in out


def test_planner_source_never_says_upgrade_head() -> None:
    assert UPGRADE_HEAD.search(SCRIPT.read_text(encoding="utf-8")) is None


# ---------------------------------------------------------------------------
# Fix loop 1: the reviewer's surviving mutants
# ---------------------------------------------------------------------------


def test_allowlist_is_exactly_pinned() -> None:
    assert wiring.ALLOWED_OPERATIONS == frozenset(
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
    assert ("ssm", "GetParameter") not in wiring.ALLOWED_OPERATIONS


def test_served_hosts_are_a_static_allowlist() -> None:
    assert wiring.SERVED_HOSTS == (
        "staging.api.neurostack.global",
        "payments.staging.api.neurostack.global",
    )


@pytest.mark.parametrize(
    "host",
    [
        "evil.neurostack.global",
        "api.neurostack.global",
        "neurostack.global",
        "prod.staging.api.neurostack.global",
        "staging.api.neurostack.global.evil.example",
        "payments-staging.api.neurostack.global",
    ],
)
def test_unserved_hosts_fail_before_any_aws_read(host: str) -> None:
    with pytest.raises(wiring.WiringError, match="parameter_invalid:ASAAS_WEBHOOK_URL"):
        _validated(ASAAS_WEBHOOK_URL=f"https://{host}/v1/billing/webhook/asaas")
    with pytest.raises(
        wiring.WiringError, match="parameter_invalid:ASAAS_CHECKOUT_RETURN_URL"
    ):
        _validated(ASAAS_CHECKOUT_RETURN_URL=f"https://{host}/billing")


@pytest.mark.parametrize("field", ["SecretString", "SecretBinary", "SecretValues"])
def test_secret_material_nested_below_the_top_level_is_rejected(
    field: str, tmp_path: Path
) -> None:
    nested = {"Stacks": [{"Outputs": [{"OutputValue": {field: CANARY_KEY}}]}]}
    aws = wiring.ReadOnlyAws(lambda *_args: copy.deepcopy(nested))
    with pytest.raises(wiring.WiringError, match="secret_material_in_readback"):
        aws.call("cloudformation", "DescribeStacks", {"StackName": "x"})
    path = FakeAws().snapshot(tmp_path / "readback.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["reads"][-1]["response"] = nested
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(wiring.WiringError, match="secret_material_in_readback"):
        wiring.snapshot_transport(path)


def test_conditional_kms_decrypt_grant_does_not_count() -> None:
    fake = FakeAws()
    document = copy.deepcopy(TERRAFORM_POLICY)
    document["Statement"][1]["Condition"] = {
        "StringEquals": {"kms:ViaService": "s3.us-east-1.amazonaws.com"}
    }
    fake.put(
        "iam",
        "GetRolePolicy",
        {"RoleName": EXECUTION_ROLE, "PolicyName": "segredos-e-logs"},
        _policy(document, "segredos-e-logs"),
    )
    plan, _rendered = _plan(fake)
    assert (
        "kms_decrypt_not_granted_for_secret_key"
        in _step(plan, "secret_access_stack")["blocks"]
    )


def test_same_named_policy_outside_the_stack_blocks() -> None:
    fake = FakeAws()
    fake.put(
        "iam",
        "GetRolePolicy",
        {"RoleName": EXECUTION_ROLE, "PolicyName": wiring.WIRING_POLICY_NAME},
        _policy(
            wiring.expected_wiring_policy([API_KEY_ARN, WEBHOOK_TOKEN_ARN, SMOKE_ARN]),
            wiring.WIRING_POLICY_NAME,
        ),
    )
    plan, _rendered = _plan(fake)
    assert (
        "wiring_policy_exists_outside_stack"
        in _step(plan, "secret_access_stack")["blocks"]
    )
    assert plan["status"] == "blocked"


@pytest.mark.parametrize("name", ["ABACATEPAY_WEBHOOK_SECRET", "APPCONFIG_TOKEN"])
def test_forbidden_names_under_secrets_block(name: str) -> None:
    secrets = _task_definition()["taskDefinition"]["containerDefinitions"][0][
        "secrets"
    ] + [{"name": name, "valueFrom": SECRET_PREFIX + "legacy-AbC123"}]
    fake = FakeAws(applied=True)
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:8", "include": ["TAGS"]},
        _task_definition(app_secrets=secrets),
    )
    plan, rendered = _plan(fake)
    assert rendered is None
    assert (
        f"rollback_target_has_forbidden_names:{name}"
        in _step(plan, "task_definition_revision")["blocks"]
    )
    request = wiring._request_from_response(_task_definition(app_secrets=secrets))
    assert wiring.forbidden_names_in(request) == [name]


# ---------------------------------------------------------------------------
# Fix loop 1: the protected deploy path (F1) is analysed from the real validators
# ---------------------------------------------------------------------------


def test_protected_path_image_repository_mismatch_is_reported() -> None:
    plan, _rendered = _plan(FakeAws(applied=True))
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "F1_protected_path_image_repository_mismatch" in reasons
    info = _step(plan, "signed_image_build_and_attestation")["info"]
    assert info["protected_path_image_repository"] == wiring.PRODUCER_IMAGE_REPOSITORY


def test_producer_repository_override_clears_the_repository_mismatch() -> None:
    digest = "sha256:" + "3" * 64
    override = f"{wiring.PRODUCER_IMAGE_REPOSITORY}@{digest}"
    fake = FakeAws(applied=True)
    fake.put(
        "ecr",
        "DescribeImages",
        {
            "repositoryName": wiring.PRODUCER_IMAGE_REPOSITORY.split("/", 1)[1],
            "imageIds": [{"imageDigest": digest}],
        },
        {"imageDetails": [{"imageDigest": digest}]},
    )
    plan, rendered = _plan(fake, app_image_override=override)
    assert rendered is not None
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "F1_protected_path_image_repository_mismatch" not in reasons


def test_path_b_fingerprint_change_is_reported_and_d3_values_are_fingerprinted() -> (
    None
):
    plan, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "F1_path_b_runtime_fingerprint_changes" in reasons
    info = _step(plan, "signed_image_build_and_attestation")["info"]
    source = wiring._request_from_response(_task_definition())
    assert info["runtime_fingerprint_rollback_target"] == (
        wiring.rollout._task_definition_runtime_fingerprint(source)
    )
    assert info["runtime_fingerprint_candidate"] == (
        wiring.rollout._task_definition_runtime_fingerprint(rendered)
    )
    # F1 path A: a changed D3 value now changes the runtime fingerprint.
    assert info["d3_environment_values_fingerprinted"] is True


def test_d3_fingerprint_probe_reports_false_when_d3_values_are_not_hashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The planner probe changes only D3 values, so it can detect a regression."""

    monkeypatch.setattr(
        rollout, "FINGERPRINTED_ENVIRONMENT_NAMES", rollout.BASE_ENVIRONMENT_NAMES
    )
    plan, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    info = _step(plan, "signed_image_build_and_attestation")["info"]
    assert info["d3_environment_values_fingerprinted"] is False


def test_digest_pinned_bridge_image_clears_the_repository_mismatch() -> None:
    plan, rendered = _plan(FakeAws(applied=True), app_image_override=OVERRIDE_IMAGE)
    assert rendered is not None
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "F1_protected_path_image_repository_mismatch" not in reasons
    # A digest in any other repository of the account is still a mismatch.
    other = OVERRIDE_IMAGE.replace("/neurostack-staging@", "/neurostack-other@")
    fake = FakeAws(applied=True)
    fake.put(
        "ecr",
        "DescribeImages",
        {
            "repositoryName": "neurostack-other",
            "imageIds": [{"imageDigest": "sha256:" + "2" * 64}],
        },
        {"imageDetails": [{"imageDigest": "sha256:" + "2" * 64}]},
    )
    plan, rendered = _plan(fake, app_image_override=other)
    assert rendered is not None
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "F1_protected_path_image_repository_mismatch" in reasons


# ---------------------------------------------------------------------------
# T2 (live chain): otel digest pin and ManagedBy=manual on new revisions
# ---------------------------------------------------------------------------

OTEL_DIGEST_PATTERN = re.compile(
    r"^public\.ecr\.aws/aws-observability/aws-otel-collector@sha256:[0-9a-f]{64}$"
)


def test_otel_image_is_a_manifest_digest_pin_of_the_live_resolution() -> None:
    manifest = wiring.validate_manifest(wiring.load_yaml(MANIFEST))
    assert manifest["task_definition"]["otel_image"] == OTEL_DIGEST_IMAGE
    assert OTEL_DIGEST_PATTERN.fullmatch(_pins().otel_image)
    # The planner holds the repository and the digest form, never the value.
    source = SCRIPT.read_text(encoding="utf-8")
    assert "7968fb60db6a2390a47ba6a2df029745638486e285c9b2487da1b722d0855a3e" not in (
        source
    )


def test_rendered_revision_9_runs_otel_by_digest_never_a_tag() -> None:
    _plan_result, rendered = _plan(
        FakeAws(applied=True), app_image_override=OVERRIDE_IMAGE
    )
    assert rendered is not None
    otel = evidence._container_map(rendered)["otel"]["image"]
    assert otel == OTEL_DIGEST_IMAGE
    assert OTEL_DIGEST_PATTERN.fullmatch(otel)
    assert ":latest" not in json.dumps(rendered)
    registered = _registered_revision_9(rendered)
    assert (
        evidence._container_map(registered["taskDefinition"])["otel"]["image"]
        == OTEL_DIGEST_IMAGE
    )


@pytest.mark.parametrize(
    "value",
    [
        "public.ecr.aws/aws-observability/aws-otel-collector:latest",
        "public.ecr.aws/aws-observability/aws-otel-collector:v0.40.0@sha256:"
        + "7" * 64,
        "public.ecr.aws/other/aws-otel-collector@sha256:" + "7" * 64,
        "public.ecr.aws/aws-observability/aws-otel-collector@sha256:" + "A" * 64,
        "public.ecr.aws/aws-observability/aws-otel-collector@sha256:" + "7" * 63,
        None,
        7,
    ],
)
def test_manifest_validator_rejects_an_otel_tag_or_other_image(value: Any) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    manifest["task_definition"]["otel_image"] = value
    with pytest.raises(wiring.WiringError, match="manifest_otel_image_not_digest"):
        wiring.validate_manifest(manifest)


def test_rendered_revision_replaces_a_terraform_managed_by_tag() -> None:
    fake = FakeAws(applied=True)
    source = _task_definition()
    source["tags"] = [
        {"key": "ManagedBy", "value": "terraform"},
        {"key": "Project", "value": "neurostack-api"},
    ]
    for request in (
        {"taskDefinition": "neurostack-staging-app:8", "include": ["TAGS"]},
        {"taskDefinition": "neurostack-staging-app", "include": ["TAGS"]},
    ):
        fake.put("ecs", "DescribeTaskDefinition", request, source)
    _plan_result, rendered = _plan(fake)
    assert rendered is not None
    assert rendered["tags"] == [
        {"key": "ManagedBy", "value": "manual"},
        {"key": "Project", "value": "neurostack-api"},
    ]


@pytest.mark.parametrize(
    "value",
    [{"ManagedBy": "terraform"}, {}, {"ManagedBy": "manual", "Owner": "x"}, None],
)
def test_manifest_validator_pins_the_managed_by_override(value: Any) -> None:
    for section in ("task_definition", "migrations_task_definition"):
        manifest = wiring.load_yaml(MANIFEST)
        manifest[section]["tag_overrides"] = value
        with pytest.raises(wiring.WiringError):
            wiring.validate_manifest(manifest)


# ---------------------------------------------------------------------------
# T2: the rollback target is a manifest field, re-pinned 8 -> 9 before 036,
# then 9 -> 10 before the image bump is deployed
# ---------------------------------------------------------------------------


# The live bridge revision 9 (read back 2026-10-22): its app image by digest.
LIVE_REVISION_9_IMAGE = (
    f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com/neurostack-staging@sha256:"
    "18358ce728c465ffd0d9cf6d0e9cf4641c32f6bab9dfa2905729c5495bcf7f24"
)


def test_rollback_target_is_re_pinned_to_revision_10_without_a_note() -> None:
    """The reviewed manifest after the second re-pin: revision 10, no note."""

    raw = wiring.load_yaml(MANIFEST)
    assert raw["rollback_target"] == {
        "revision": 10,
        "effective_after": wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG,
    }
    assert wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG == (
        "worker_flag_revision_10_serving_verified_and_observation_closed"
    )
    manifest = wiring.validate_manifest(raw)
    assert "repin" not in manifest["rollback_target"]
    pins = _pins()
    assert pins.rollback_revision == wiring.WORKER_FLAG_REVISION == 10
    assert pins.rollback_task_definition_arn == REVISION_10_ARN
    assert pins.rollback_effective_after == wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG
    assert pins.rollback_repin_before is None
    # The D3 source stays revision 8: the re-pins move only the rollback target.
    assert manifest["task_definition"]["source_revision"] == 8
    # The pins form one chain, 8 -> 9 -> 10, and 10 is the last.
    assert wiring.ROLLBACK_TARGET_PINS == {
        8: (
            wiring.ROLLBACK_EFFECTIVE_INITIAL,
            (9, wiring.ROLLBACK_EFFECTIVE_BRIDGE, "migration_036"),
        ),
        9: (
            wiring.ROLLBACK_EFFECTIVE_BRIDGE,
            (10, wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG, "app_image_bump_deployment"),
        ),
        10: (wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG, None),
    }


def test_revision_9_is_a_pin_with_its_pending_re_pin_to_10() -> None:
    """Revision 9 still validates, but only with its note naming 10."""

    pins = _worker_pins(9)
    assert pins.rollback_revision == wiring.BRIDGE_REVISION == 9
    assert pins.rollback_task_definition_arn == NEXT_ARN
    assert pins.rollback_effective_after == wiring.ROLLBACK_EFFECTIVE_BRIDGE
    assert pins.rollback_repin_before == "app_image_bump_deployment"
    manifest = wiring.load_yaml(MANIFEST)
    manifest["rollback_target"] = {
        "revision": 9,
        "effective_after": wiring.ROLLBACK_EFFECTIVE_BRIDGE,
    }
    with pytest.raises(wiring.WiringError, match="manifest_rollback_target_invalid"):
        wiring.validate_manifest(manifest)


def test_the_manifest_before_the_re_pin_still_validates() -> None:
    pins = _initial_pins()
    assert pins.rollback_revision == 8
    assert pins.rollback_task_definition_arn == SOURCE_ARN
    assert pins.rollback_repin_before == "migration_036"


def test_repinned_rollback_target_9_flows_through_the_plan() -> None:
    pins = _worker_pins(9)
    assert pins.rollback_revision == 9
    assert pins.rollback_repin_before == "app_image_bump_deployment"
    fake = FakeAws(applied=True)
    _plan_result, rendered = _plan(fake, app_image_override=OVERRIDE_IMAGE)
    assert rendered is not None
    registered = _registered_revision_9(rendered)
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:9", "include": ["TAGS"]},
        registered,
    )
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app", "include": ["TAGS"]},
        registered,
    )
    fake.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(NEXT_ARN),
    )
    fake.calls.clear()
    plan, _rendered = _plan(fake, pins=pins, app_image_override=OVERRIDE_IMAGE)
    assert plan["target"]["rollback_target"] == NEXT_ARN
    deployment = _step(plan, "service_deployment")
    assert deployment["info"]["rollback_target"] == NEXT_ARN
    assert deployment["info"]["rollback_target_must_repin_before"] == (
        "app_image_bump_deployment"
    )
    assert "rollback_target_active_and_digest_pinned" in deployment["compliant"]
    # The D3 source stays revision 8: revision 9 is still derived from it.
    assert plan["task_definition_diff"]["source_task_definition_arn"] == SOURCE_ARN
    assert (
        f"registered_and_deployed:{NEXT_ARN}"
        in _step(plan, "task_definition_revision")["compliant"]
    )
    reads = [p for _s, o, p in fake.calls if o == "DescribeTaskDefinition"]
    assert {"taskDefinition": "neurostack-staging-app:9", "include": ["TAGS"]} in reads


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({}, None),
        ({"status": "INACTIVE"}, "rollback_target_revision_unusable"),
        ({"image": LIVE_IMAGE}, "rollback_target_revision_unusable"),
    ],
)
def test_repinned_rollback_target_must_be_active_and_digest_pinned(
    changes: dict[str, Any], reason: str | None
) -> None:
    fake = FakeAws(applied=True)
    target = _task_definition(
        REVISION_10_ARN, image=changes.get("image", LIVE_REVISION_9_IMAGE)
    )
    if "status" in changes:
        target["taskDefinition"]["status"] = changes["status"]
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:10", "include": ["TAGS"]},
        target,
    )
    plan, _rendered = _plan(fake, pins=_pins())
    assert plan["target"]["rollback_target"] == REVISION_10_ARN
    deployment = _step(plan, "service_deployment")
    if reason is None:
        assert "rollback_target_active_and_digest_pinned" in deployment["compliant"]
        assert deployment["info"]["rollback_target_image_pinned_by_digest"] is True
    else:
        assert reason in deployment["blocks"]


def _set_rollback(**values: Any) -> Callable[[dict[str, Any]], None]:
    return lambda m: m["rollback_target"].update(values)


@pytest.mark.parametrize(
    "mutate",
    [
        _set_rollback(revision=7),
        _set_rollback(revision=8),
        _set_rollback(revision=9),
        _set_rollback(revision=11),
        _set_rollback(revision=True),
        _set_rollback(revision="9"),
        _set_rollback(effective_after="whenever"),
        _set_rollback(effective_after=wiring.ROLLBACK_EFFECTIVE_INITIAL),
        _set_rollback(repin=copy.deepcopy(INITIAL_ROLLBACK_TARGET["repin"])),
        lambda m: m["rollback_target"].__setitem__("note", "x"),
        lambda m: m["rollback_target"].pop("effective_after"),
        lambda m: m.pop("rollback_target"),
    ],
)
def test_manifest_validator_rejects_an_invalid_rollback_target(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    mutate(manifest)
    with pytest.raises(wiring.WiringError):
        wiring.validate_manifest(manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        _set_rollback(effective_after="whenever"),
        lambda m: m["rollback_target"].pop("repin"),
        lambda m: m["rollback_target"]["repin"].__setitem__("before", "migration_037"),
        lambda m: m["rollback_target"]["repin"].__setitem__("revision", 10),
        lambda m: m["rollback_target"]["repin"].__setitem__("why", ""),
        lambda m: m["rollback_target"]["repin"].__setitem__(
            "effective_after", wiring.ROLLBACK_EFFECTIVE_INITIAL
        ),
    ],
)
def test_manifest_validator_rejected_an_invalid_initial_rollback_target(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = _initial_manifest()
    wiring.validate_manifest(copy.deepcopy(manifest))
    mutate(manifest)
    with pytest.raises(wiring.WiringError, match="manifest_rollback_target_invalid"):
        wiring.validate_manifest(manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["rollback_target"].__setitem__(
            "repin", {"revision": 10, "effective_after": "x", "before": "y", "why": "z"}
        ),
        lambda m: m["rollback_target"].__setitem__(
            "effective_after", wiring.ROLLBACK_EFFECTIVE_INITIAL
        ),
    ],
)
def test_a_repinned_rollback_target_cannot_be_repinned_again(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    wiring.validate_manifest(copy.deepcopy(manifest))
    mutate(manifest)
    with pytest.raises(wiring.WiringError, match="manifest_rollback_target_invalid"):
        wiring.validate_manifest(manifest)


def test_planner_code_never_hardcodes_the_rollback_revision() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ROLLBACK_REVISION" not in source
    assert "ROLLBACK_TASK_DEFINITION_ARN" not in source
    # Every use of the rollback target goes through the manifest pins.
    tree = ast.parse(source)
    reads = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("rollback_")
    }
    assert "rollback_task_definition_arn" in reads


# ---------------------------------------------------------------------------
# T2: the migrations revision bakes exactly the pinned upgrade
# ---------------------------------------------------------------------------


def _migrations(
    fake: FakeAws | None = None,
    *,
    window: Any = mw.WINDOW_035,
    purpose: str = "migrate",
    **changes: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    parameters = _validated(**({"app_image_override": OVERRIDE_IMAGE} | changes))
    aws = wiring.ReadOnlyAws((fake or FakeAws(applied=True)).transport)
    readback = wiring.collect_migrations_readback(aws, parameters)
    return wiring.plan_migrations(
        readback, parameters, pins=_pins(), window=window, purpose=purpose
    )


def _put_migrations(
    fake: FakeAws, response: dict[str, Any], *, latest: bool = False
) -> None:
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {
            "taskDefinition": "neurostack-staging-migrations"
            + ("" if latest else ":3"),
            "include": ["TAGS"],
        },
        response,
    )


def test_migrations_revision_bakes_the_pinned_upgrade_on_the_bridge_image() -> None:
    status, rendered = _migrations()
    assert status["blocks"] == [], status
    assert status["state"] == "would_register"
    assert rendered is not None
    (container,) = rendered["containerDefinitions"]
    assert container["command"] == ["alembic", "upgrade", "035"]
    assert container["command"] == list(mw.WINDOW_035.command)
    assert container["image"] == OVERRIDE_IMAGE
    assert container["secrets"] == [
        {"name": "DATABASE_URL", "valueFrom": MIGRATIONS_DB_ARN}
    ]
    source = wiring._request_from_response(_migrations_task_definition())
    assert rendered["family"] == "neurostack-staging-migrations"
    assert rendered["executionRoleArn"] == source["executionRoleArn"]
    assert rendered["taskRoleArn"] == source["taskRoleArn"]
    assert {
        k: v for k, v in rendered.items() if k not in {"containerDefinitions", "tags"}
    } == {k: v for k, v in source.items() if k not in {"containerDefinitions", "tags"}}
    assert {k: v for k, v in container.items() if k not in {"image", "command"}} == {
        k: v
        for k, v in source["containerDefinitions"][0].items()
        if k not in {"image", "command"}
    }
    assert rendered["tags"] == [
        {"key": "ManagedBy", "value": "manual"},
        {"key": "Project", "value": "neurostack-api"},
    ]
    # A RegisterTaskDefinition request: never revision 3 (it bakes the head).
    text = json.dumps(rendered)
    assert "neurostack-staging-migrations:3" not in text
    assert not set(rendered) & evidence._RESPONSE_ONLY_FIELDS
    assert UPGRADE_HEAD.search(text) is None


@pytest.mark.parametrize(
    "command",
    [
        ("alembic", "upgrade", "head"),
        ("alembic", "upgrade", "heads"),
        ("alembic", "upgrade", "+1"),
        ("alembic", "upgrade", "036"),
        ("alembic", "upgrade", "037"),
        ("alembic", "upgrade", "0350"),
        ("alembic", "upgrade", "035", "--sql"),
        ("sh", "-c", "alembic upgrade 035"),
        ("alembic", "downgrade", "035"),
        (),
    ],
)
def test_migrations_render_refuses_any_command_but_the_pinned_upgrade(
    command: tuple[str, ...],
) -> None:
    with pytest.raises(wiring.WiringError, match="migrations_command_not_pinned"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=OVERRIDE_IMAGE,
            pins=_pins(),
            window=mw.WINDOW_035,
            purpose="migrate",
            command=command,
        )


@pytest.mark.parametrize(
    "widened",
    [
        {"command": ("alembic", "upgrade", "heads")},
        {"command": ("alembic", "upgrade", "head")},
        {"command": ("alembic", "upgrade", "035:037")},
        {"command": ("alembic", "upgrade", "+1")},
        {"after": "35", "command": ("alembic", "upgrade", "35")},
        {"after": "037", "command": ("alembic", "upgrade", "037")},
    ],
)
def test_migrations_render_refuses_a_widened_window_table(
    widened: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _pins()
    window = dataclasses.replace(mw.WINDOW_035, **widened)
    table = dict(mw.WINDOWS) | {"035": window}
    monkeypatch.setattr(wiring, "MIGRATION_WINDOWS", table)
    if window.command == ("alembic", "upgrade", window.after) and window.after == "037":
        # A consistent but different table entry renders its own command;
        # the manifest validator is what refuses it (the step says 035).
        with pytest.raises(wiring.WiringError, match="manifest_migrations_window"):
            _pins()
        return
    with pytest.raises(wiring.WiringError, match="migrations_command_not_pinned"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=OVERRIDE_IMAGE,
            pins=pins,
            window=window,
            purpose="migrate",
        )
    # The manifest validator refuses the widened window on its own as well.
    with pytest.raises(wiring.WiringError, match="manifest_migrations_window"):
        _pins()


@pytest.mark.parametrize("name", ["035", "036", "037"])
def test_a_window_outside_the_table_is_refused(name: str) -> None:
    """Only the table's own entries render: an equal-looking copy with any
    field changed is not a window."""

    impostor = dataclasses.replace(mw.WINDOWS[name], started_by="someone-else")
    with pytest.raises(wiring.WiringError, match="migrations_command_not_pinned"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=OVERRIDE_IMAGE,
            pins=_pins(),
            window=impostor,
            purpose="migrate",
        )
    with pytest.raises(wiring.WiringError, match="migrations_command_not_pinned"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=OVERRIDE_IMAGE,
            pins=_pins(),
            window=name,  # type: ignore[arg-type]
            purpose="migrate",
        )


def _migrations_with(**changes: Any) -> dict[str, Any]:
    return _migrations_task_definition(**changes)


MIGRATIONS_SOURCE_PROBLEMS: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
    "revision_4_answered": (
        lambda: _migrations_task_definition(MIGRATIONS_NEXT_ARN),
        "migrations_source_revision_mismatch",
    ),
    "inactive": (
        lambda: {
            **_migrations_task_definition(),
            "taskDefinition": {
                **_migrations_task_definition()["taskDefinition"],
                "status": "INACTIVE",
            },
        },
        "migrations_source_not_active",
    ),
    "entry_point": (
        lambda: _migrations_with(entryPoint=["sh", "-c"]),
        "migrations_source_entry_point_unexpected",
    ),
    "two_containers": (
        lambda: _migrations_task_definition(
            containers=[
                _migrations_task_definition()["taskDefinition"]["containerDefinitions"][
                    0
                ],
                {"name": "sidecar", "image": LIVE_IMAGE},
            ]
        ),
        "migrations_source_container_count_invalid",
    ),
    "app_database_secret": (
        lambda: _migrations_with(
            secrets=[
                {
                    "name": "DATABASE_URL",
                    "valueFrom": SECRET_PREFIX + "database-url-fyOYuj",
                }
            ]
        ),
        "migrations_database_secret_mismatch",
    ),
    "pinned_database_reference": (
        lambda: _migrations_with(
            secrets=[
                {
                    "name": "DATABASE_URL",
                    "valueFrom": MIGRATIONS_DB_ARN
                    + ":::12345678-1234-5678-1234-567812345678",
                }
            ]
        ),
        "migrations_database_secret_mismatch",
    ),
    "no_database_secret": (
        lambda: _migrations_with(secrets=[]),
        "migrations_database_secret_mismatch",
    ),
    "database_url_in_environment": (
        lambda: _migrations_with(
            environment=[{"name": "DATABASE_URL", "value": "postgresql://x"}]
        ),
        "migrations_database_secret_mismatch",
    ),
    "forbidden_name": (
        lambda: _migrations_with(
            environment=[{"name": "APPCONFIG_ENABLED", "value": "true"}]
        ),
        "forbidden_name_in_rendering",
    ),
    "execution_role": (
        lambda: {
            **_migrations_task_definition(),
            "taskDefinition": {
                **_migrations_task_definition()["taskDefinition"],
                "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/other",
            },
        },
        "migrations_source_roles_mismatch",
    ),
    "task_role": (
        lambda: {
            **_migrations_task_definition(),
            "taskDefinition": {
                **_migrations_task_definition()["taskDefinition"],
                "taskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/admin",
            },
        },
        "migrations_source_roles_mismatch",
    ),
}


@pytest.mark.parametrize("case", sorted(MIGRATIONS_SOURCE_PROBLEMS))
def test_migrations_render_refuses_an_unexpected_revision_3(case: str) -> None:
    make, reason = MIGRATIONS_SOURCE_PROBLEMS[case]
    fake = FakeAws(applied=True)
    _put_migrations(fake, make())
    status, rendered = _migrations(fake)
    assert rendered is None
    assert any(block.startswith(reason) for block in status["blocks"]), status


def test_migrations_render_requires_the_reviewed_bridge_image() -> None:
    with pytest.raises(
        wiring.WiringError, match="migrations_image_requires_app_image_override"
    ):
        _migrations(app_image_override=None)
    fake = FakeAws(applied=True)
    fake.put(
        "ecr",
        "DescribeImages",
        {
            "repositoryName": "neurostack-staging",
            "imageIds": [{"imageDigest": "sha256:" + "2" * 64}],
        },
        {"__error_code__": "ImageNotFoundException"},
    )
    status, rendered = _migrations(fake)
    assert rendered is None
    assert "migrations_image_not_found_in_ecr" in status["blocks"]


@pytest.mark.parametrize(
    "image",
    [
        LIVE_IMAGE,
        "docker.io/library/python@sha256:" + "2" * 64,
        OVERRIDE_IMAGE.replace("sha256:", "sha256:A"),
        OVERRIDE_IMAGE.replace("@sha256:", ":tag@sha256:"),
        "",
    ],
)
def test_migrations_render_refuses_an_image_not_pinned_by_digest(image: str) -> None:
    with pytest.raises(wiring.WiringError, match="migrations_image_not_digest_pinned"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=image,
            pins=_pins(),
            window=mw.WINDOW_035,
            purpose="migrate",
        )


def test_migrations_registered_revision_is_reported_and_a_differing_one_blocks() -> (
    None
):
    fake = FakeAws(applied=True)
    _status, rendered = _migrations(fake)
    assert rendered is not None
    registered = copy.deepcopy(_migrations_task_definition(MIGRATIONS_NEXT_ARN))
    registered["taskDefinition"]["containerDefinitions"] = copy.deepcopy(
        rendered["containerDefinitions"]
    )
    registered["tags"] = copy.deepcopy(rendered["tags"])
    _put_migrations(fake, registered, latest=True)
    status, again = _migrations(fake)
    assert again == rendered
    assert status["state"] == f"registered:{MIGRATIONS_NEXT_ARN}"
    assert status["run_task_definition"] == MIGRATIONS_NEXT_ARN
    _put_migrations(fake, _migrations_task_definition(MIGRATIONS_NEXT_ARN), latest=True)
    status, again = _migrations(fake)
    assert again is None
    assert (
        f"migrations_newer_revision_differs:{MIGRATIONS_NEXT_ARN}" in status["blocks"]
    )


def test_cli_render_migrations_taskdef_from_a_readback_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws(applied=True).snapshot(tmp_path / "readback.json")
    params = _write_params(tmp_path, app_image_override=OVERRIDE_IMAGE)
    code, out, err = _run(
        [
            "render-migrations-taskdef",
            "--window",
            "035",
            "--purpose",
            "migrate",
            "--params",
            str(params),
            "--readback-file",
            str(snapshot),
        ],
        capsys,
    )
    assert code == 0, err
    rendered = json.loads(out)
    assert rendered["containerDefinitions"][0]["command"] == [
        "alembic",
        "upgrade",
        "035",
    ]
    assert UPGRADE_HEAD.search(out + err) is None
    assert "neurostack-staging-migrations:3" not in out
    params = _write_params(tmp_path)
    code, out, err = _run(
        [
            "render-migrations-taskdef",
            "--window",
            "035",
            "--purpose",
            "migrate",
            "--params",
            str(params),
            "--readback-file",
            str(snapshot),
        ],
        capsys,
    )
    assert code == 2
    assert out == ""
    assert "blocked: migrations_image_requires_app_image_override" in err


# ---------------------------------------------------------------------------
# T2: F1 known block names the open residual, and its text is current
# ---------------------------------------------------------------------------


def test_known_block_ids_name_open_blocks_only() -> None:
    assert wiring.KNOWN_BLOCKS == {
        "F1_residual_5_database_url_fingerprint": "signed_image_build_and_attestation",
        "F3_webhook_registration_entry_point": "webhook_exposure_and_registration",
    }
    manifest = wiring.validate_manifest(wiring.load_yaml(MANIFEST))
    assert {row["id"] for row in manifest["known_blocks"]} == set(wiring.KNOWN_BLOCKS)
    for text in (
        MANIFEST.read_text(encoding="utf-8"),
        SCRIPT.read_text(encoding="utf-8"),
    ):
        assert "F1_group5_deploy_validator_allowlist" not in text


def _plain_and_pinned_fingerprints_differ(request: dict[str, Any]) -> bool:
    """What the protected deploy compares: plain ARN serving vs pinned replacement."""

    pinned = copy.deepcopy(request)
    for row in evidence._container_map(pinned)["app"]["secrets"]:
        if row["name"] == "DATABASE_URL":
            row["valueFrom"] += ":::12345678-1234-5678-1234-567812345678"
    return bool(
        rollout._task_definition_runtime_fingerprint(request)
        != rollout._task_definition_runtime_fingerprint(pinned)
    )


def test_residual_5_is_reported_from_the_real_fingerprint() -> None:
    """Present iff the real fingerprint differs (true until PR #118 merges)."""

    plan, rendered = _plan(FakeAws(applied=True), app_image_override=OVERRIDE_IMAGE)
    assert rendered is not None
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert ("F1_residual_5_database_url_fingerprint" in reasons) is (
        _plain_and_pinned_fingerprints_differ(rendered)
    )
    assert not any(reason.startswith("F1_group5") for reason in reasons)
    info = _step(plan, "signed_image_build_and_attestation")["info"]
    assert info["protected_replacement_keeps_runtime_fingerprint"] is (
        not _plain_and_pinned_fingerprints_differ(rendered)
    )


def test_residual_5_probe_reports_a_difference_when_the_fingerprint_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced difference: the probe must report it whatever main ships."""

    original = rollout._task_definition_runtime_fingerprint

    def strict(definition: dict[str, Any]) -> str:
        app = evidence._container_map(definition)["app"]
        database = [r for r in app["secrets"] if r["name"] == "DATABASE_URL"]
        return str(original(definition)) + str(database[0]["valueFrom"])

    monkeypatch.setattr(rollout, "_task_definition_runtime_fingerprint", strict)
    plan, rendered = _plan(FakeAws(applied=True), app_image_override=OVERRIDE_IMAGE)
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "F1_residual_5_database_url_fingerprint" in reasons


@pytest.mark.parametrize("present", [True, False])
def test_f3_block_follows_the_entry_point_file(
    present: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        wiring,
        "WEBHOOK_REGISTRATION_ENTRY_POINT",
        "scripts/asaas_production_wiring.py" if present else "src/ops/absent.py",
    )
    plan, _rendered = _plan(FakeAws(applied=True))
    webhook = _step(plan, "webhook_exposure_and_registration")
    assert ("F3_webhook_registration_entry_point:missing" in webhook["blocks"]) is (
        not present
    )
    assert ("webhook_registration_entry_point_present" in webhook["compliant"]) is (
        present
    )


def test_rollback_target_8_is_never_reported_as_digest_pinned() -> None:
    plan, _rendered = _plan(FakeAws(applied=True))
    deployment = _step(plan, "service_deployment")
    assert "rollback_target_active_and_digest_pinned" not in deployment["compliant"]
    assert (
        "rollback_target_active_on_initial_tagged_revision" in deployment["compliant"]
    )
    assert deployment["info"]["rollback_target_image_pinned_by_digest"] is False


def test_migrations_fault_injection_false_is_dropped_and_true_refused() -> None:
    fake = FakeAws(applied=True)
    off = _migrations_task_definition()
    off["taskDefinition"]["enableFaultInjection"] = False
    _put_migrations(fake, off)
    status, rendered = _migrations(fake)
    assert rendered is not None and status["blocks"] == []
    assert "enableFaultInjection" not in rendered
    on = _migrations_task_definition()
    on["taskDefinition"]["enableFaultInjection"] = True
    _put_migrations(fake, on)
    status, rendered = _migrations(fake)
    assert rendered is None
    assert "migrations_source_fault_injection_not_false" in status["blocks"]


def test_migrations_registered_with_fault_injection_false_still_matches() -> None:
    fake = FakeAws(applied=True)
    _status, rendered = _migrations(fake)
    assert rendered is not None
    registered = copy.deepcopy(_migrations_task_definition(MIGRATIONS_NEXT_ARN))
    registered["taskDefinition"]["containerDefinitions"] = copy.deepcopy(
        rendered["containerDefinitions"]
    )
    registered["tags"] = copy.deepcopy(rendered["tags"])
    registered["taskDefinition"]["enableFaultInjection"] = False
    _put_migrations(fake, registered, latest=True)
    status, _again = _migrations(fake)
    assert status["state"] == f"registered:{MIGRATIONS_NEXT_ARN}"


def test_cli_render_migrations_refuses_to_register_an_identical_revision_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeAws(applied=True)
    _status, rendered = _migrations(fake)
    assert rendered is not None
    registered = copy.deepcopy(_migrations_task_definition(MIGRATIONS_NEXT_ARN))
    registered["taskDefinition"]["containerDefinitions"] = copy.deepcopy(
        rendered["containerDefinitions"]
    )
    registered["tags"] = copy.deepcopy(rendered["tags"])
    _put_migrations(fake, registered, latest=True)
    snapshot = fake.snapshot(tmp_path / "readback.json")
    params = _write_params(tmp_path, app_image_override=OVERRIDE_IMAGE)
    args = [
        "render-migrations-taskdef",
        "--window",
        "035",
        "--purpose",
        "migrate",
        "--params",
        str(params),
        "--readback-file",
        str(snapshot),
    ]
    code, out, err = _run(args, capsys)
    assert code == 3
    assert out == ""
    assert f"already registered: {MIGRATIONS_NEXT_ARN}" in err
    code, out, err = _run([*args, "--force-new-revision"], capsys)
    assert code == 0, err
    assert json.loads(out) == rendered


def test_residual_5_block_clears_once_the_fingerprint_normalizes_the_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = rollout._task_definition_runtime_fingerprint

    def fixed(definition: dict[str, Any]) -> str:
        probe = copy.deepcopy(definition)
        for container in probe.get("containerDefinitions") or []:
            for secret in container.get("secrets") or []:
                if secret.get("name") == "DATABASE_URL":
                    secret["valueFrom"] = ":".join(secret["valueFrom"].split(":")[:7])
        return str(original(probe))

    monkeypatch.setattr(rollout, "_task_definition_runtime_fingerprint", fixed)
    plan, rendered = _plan(FakeAws(applied=True), app_image_override=OVERRIDE_IMAGE)
    assert rendered is not None
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert not any(reason.startswith("F1_residual_5") for reason in reasons)


def test_group5_template_regression_is_still_reported_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        wiring, "group5_template_verdict", lambda *_a: (False, "names_mismatch")
    )
    plan, _rendered = _plan(FakeAws(applied=True))
    reasons = {row["reason"] for row in plan["gate_blocks"]}
    assert "F1_group5_template_validator_regression:names_mismatch" in reasons


def test_manifest_f1_and_deployment_text_is_current() -> None:
    text = " ".join(MANIFEST.read_text(encoding="utf-8").split())
    for stale in (
        "rejects it even without D3",
        "if the owner approves",
        "The deploy-path code is not changed here",
        "pin the app environment and secret names to the revision 8 sets",
        "only the six base environment values",
        "it does not exist today",
        "derived from the live rollback target",
    ):
        assert stale not in text, stale
    raw = MANIFEST.read_text(encoding="utf-8")
    # The owner confirmed ManagedBy=manual on 2026-10-20 (roles and new
    # task-definition revisions): no pending wording is left.
    assert "PENDING OWNER CONFIRMATION" not in raw.upper()
    assert raw.count("OWNER CONFIRMED") == 2
    assert "every new\n  # task-definition revision, app and migrations alike" in raw
    assert "PR #118" in text and "PR #120" in text
    for current in (
        "residual 5",
        "mutable tag",
        "A1",
        "readback_runtime_fingerprint_sha256_changed",
        "scripts/asaas_deploy_hook.py",
        "DEPLOYED_WEBHOOK_PENDING",
    ):
        assert current in text, current


# ---------------------------------------------------------------------------
# Final loop: the newest migrations revision must be ours before reuse
# ---------------------------------------------------------------------------


def test_migrations_fixture_arns_keep_the_account() -> None:
    for arn in (MIGRATIONS_SOURCE_ARN, MIGRATIONS_NEXT_ARN, NEXT_ARN, SOURCE_ARN):
        assert arn.startswith(f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/")
    assert MIGRATIONS_NEXT_ARN.endswith("/neurostack-staging-migrations:4")


def _registered_migrations(arn: str, rendered: dict[str, Any], **changes: Any) -> Any:
    registered = copy.deepcopy(_migrations_task_definition(MIGRATIONS_NEXT_ARN))
    registered["taskDefinition"]["containerDefinitions"] = copy.deepcopy(
        rendered["containerDefinitions"]
    )
    registered["taskDefinition"]["taskDefinitionArn"] = arn
    registered["taskDefinition"].update(changes)
    registered["tags"] = copy.deepcopy(rendered["tags"])
    return registered


@pytest.mark.parametrize(
    "arn,changes",
    [
        (MIGRATIONS_NEXT_ARN.replace(f":{ACCOUNT}:", ":488460142431:"), {}),
        (MIGRATIONS_NEXT_ARN.replace(":us-east-1:", ":us-east-2:"), {}),
        (
            MIGRATIONS_NEXT_ARN.replace(
                "neurostack-staging-migrations", "other-family"
            ),
            {},
        ),
        (MIGRATIONS_NEXT_ARN.rsplit(":", 1)[0] + ":2", {"revision": 2}),
        (MIGRATIONS_NEXT_ARN, {"revision": 5}),
        (MIGRATIONS_NEXT_ARN, {"family": "other-family"}),
        (MIGRATIONS_NEXT_ARN + "x", {}),
    ],
)
def test_an_equal_newest_revision_outside_this_family_is_never_reused(
    arn: str,
    changes: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeAws(applied=True)
    _status, rendered = _migrations(fake)
    assert rendered is not None
    _put_migrations(fake, _registered_migrations(arn, rendered, **changes), latest=True)
    status, again = _migrations(fake)
    assert again is None
    assert status["run_task_definition"] is None
    assert "migrations_newest_revision_invalid" in status["blocks"]
    snapshot = fake.snapshot(tmp_path / "readback.json")
    params = _write_params(tmp_path, app_image_override=OVERRIDE_IMAGE)
    code, out, err = _run(
        [
            "render-migrations-taskdef",
            "--window",
            "035",
            "--purpose",
            "migrate",
            "--params",
            str(params),
            "--readback-file",
            str(snapshot),
        ],
        capsys,
    )
    assert code == 2 and out == ""
    assert "already registered" not in err
    assert "migrations_newest_revision_invalid" in err


def test_an_equal_newest_revision_of_this_family_is_reported(
    tmp_path: Path,
) -> None:
    fake = FakeAws(applied=True)
    _status, rendered = _migrations(fake)
    assert rendered is not None
    _put_migrations(
        fake, _registered_migrations(MIGRATIONS_NEXT_ARN, rendered), latest=True
    )
    status, _again = _migrations(fake)
    assert status["run_task_definition"] == MIGRATIONS_NEXT_ARN


# ---------------------------------------------------------------------------
# T8: three explicit windows (035, 036, 037), one table, no default window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["035", "036", "037"])
def test_each_window_renders_only_its_own_baked_upgrade(name: str) -> None:
    window = mw.WINDOWS[name]
    status, rendered = _migrations(window=window)
    assert status["blocks"] == [], status
    assert status["window"] == name
    assert status["command"] == f"alembic upgrade {name}"
    assert rendered is not None
    (container,) = rendered["containerDefinitions"]
    assert container["command"] == ["alembic", "upgrade", name]
    text = json.dumps(rendered)
    assert UPGRADE_HEAD.search(text) is None
    assert "neurostack-staging-migrations:3" not in text
    others = {other for other in mw.WINDOWS if other != name}
    assert not any(f'"upgrade", "{other}"' in text for other in others)


@pytest.mark.parametrize(
    "name,command",
    [
        (name, command)
        for name in ("035", "036", "037")
        for command in (
            ("alembic", "upgrade", "head"),
            ("alembic", "upgrade", "heads"),
            ("alembic", "upgrade", "035:037"),
            ("alembic", "upgrade", "+1"),
            *[
                ("alembic", "upgrade", other)
                for other in ("035", "036", "037")
                if other != name
            ],
        )
    ],
)
def test_a_window_refuses_every_other_command(
    name: str, command: tuple[str, ...]
) -> None:
    with pytest.raises(wiring.WiringError, match="migrations_command_not_pinned"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=OVERRIDE_IMAGE,
            pins=_pins(),
            window=mw.WINDOWS[name],
            purpose="migrate",
            command=command,
        )


def _registered_for(window: Any, arn: str = MIGRATIONS_NEXT_ARN) -> Any:
    _status, rendered = _migrations(window=window)
    assert rendered is not None
    return _registered_migrations(arn, rendered)


@pytest.mark.parametrize(
    "newest,target,state_or_block",
    [
        ("035", "036", "would_register"),
        ("035", "037", "would_register"),
        ("036", "037", "would_register"),
        ("036", "035", "migrations_newest_revision_is_a_later_window"),
        ("037", "035", "migrations_newest_revision_is_a_later_window"),
        ("037", "036", "migrations_newest_revision_is_a_later_window"),
        ("036", "036", "registered"),
    ],
)
def test_the_newest_revision_of_another_window_is_never_reused(
    newest: str, target: str, state_or_block: str
) -> None:
    fake = FakeAws(applied=True)
    _put_migrations(fake, _registered_for(mw.WINDOWS[newest]), latest=True)
    status, rendered = _migrations(fake, window=mw.WINDOWS[target])
    if state_or_block == "would_register":
        assert status["state"] == "would_register", status
        assert status["newest_revision_window"] == newest
        assert status["run_task_definition"] is None
        assert rendered is not None
        assert rendered["containerDefinitions"][0]["command"] == [
            "alembic",
            "upgrade",
            target,
        ]
    elif state_or_block == "registered":
        assert status["state"] == f"registered:{MIGRATIONS_NEXT_ARN}"
        assert status["run_task_definition"] == MIGRATIONS_NEXT_ARN
    else:
        assert rendered is None
        assert f"{state_or_block}:{MIGRATIONS_NEXT_ARN}" in status["blocks"]
        assert status["run_task_definition"] is None


def test_an_earlier_window_revision_that_was_tampered_with_blocks() -> None:
    fake = FakeAws(applied=True)
    tampered = _registered_for(mw.WINDOW_035)
    tampered["taskDefinition"]["containerDefinitions"][0]["environment"] = [
        {"name": "EXTRA", "value": "1"}
    ]
    _put_migrations(fake, tampered, latest=True)
    status, rendered = _migrations(fake, window=mw.WINDOW_036)
    assert rendered is None
    assert (
        f"migrations_newer_revision_differs:{MIGRATIONS_NEXT_ARN}" in status["blocks"]
    )


def test_cli_render_migrations_taskdef_requires_an_explicit_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws(applied=True).snapshot(tmp_path / "readback.json")
    params = _write_params(tmp_path, app_image_override=OVERRIDE_IMAGE)
    base = ["--params", str(params), "--readback-file", str(snapshot)]
    for window in (None, "", "head", "034", "038", "035:037"):
        argv = ["render-migrations-taskdef", *base]
        if window is not None:
            argv += ["--window", window]
        with pytest.raises(SystemExit) as raised:
            wiring.main(argv)
        assert raised.value.code == 2
        assert capsys.readouterr().out == ""
    for name in ("035", "036", "037"):
        code, out, err = _run(
            [
                "render-migrations-taskdef",
                "--window",
                name,
                "--purpose",
                "migrate",
                *base,
            ],
            capsys,
        )
        assert code == 0, err
        assert json.loads(out)["containerDefinitions"][0]["command"] == [
            "alembic",
            "upgrade",
            name,
        ]


def test_plan_reports_every_window_step_from_the_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws(applied=True).snapshot(tmp_path / "readback.json")
    code, out, err = _run(
        [
            "plan",
            "--params",
            str(PARAMS),
            "--readback-file",
            str(snapshot),
            "--manifest",
            str(_initial_manifest_file(tmp_path)),
        ],
        capsys,
    )
    assert code == 0, err
    plan = json.loads(out)
    for window in mw.WINDOWS.values():
        step = _step(plan, window.manifest_step)
        assert step["scope"] == "separate_approval"
        assert step["state"] == "not_performed"
        info = step["info"]
        assert info["window"] == window.name
        assert info["command"] == f"alembic upgrade {window.after}"
        assert info["required_current_revision"] == window.before
        assert info["target_revision"] == window.after
        assert info["verification"] == window.verification
        assert info["snapshot_name_pattern"] == window.snapshot_pattern
        assert info["run_tooling"] == (
            f"scripts/asaas_migration_window.py --window {window.name}"
        )
    assert UPGRADE_HEAD.search(out) is None


def _window_step(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    step_id = mw.WINDOWS[name].manifest_step
    return next(row for row in manifest["steps"] if row["id"] == step_id)


@pytest.mark.parametrize(
    "name,mutate",
    [
        (name, mutate)
        for name in ("036", "037")
        for mutate in (
            lambda s, n: s.__setitem__(
                "change",
                s["change"].replace(f"`alembic upgrade {n}`", "`alembic upgrade head`"),
            ),
            lambda s, n: s.__setitem__(
                "change", s["change"].replace(f"--window {n}", "--window 035")
            ),
            lambda s, n: s.__setitem__(
                "change",
                (
                    s["change"] + " then `alembic upgrade 037`"
                    if n == "036"
                    else s["change"] + " then `alembic upgrade 036`"
                ),
            ),
            lambda s, n: s.__setitem__(
                "preconditions",
                [
                    row.replace(
                        f"alembic_version == {mw.WINDOWS[n].before}",
                        "alembic_version == 032",
                    )
                    for row in s["preconditions"]
                ],
            ),
            lambda s, n: s.__setitem__(
                "verification",
                [
                    row.replace(f"alembic_version == {n}", "alembic_version == 035")
                    for row in s["verification"]
                ],
            ),
            lambda s, n: s.__setitem__(
                "preconditions",
                [
                    row.replace(f"pre-{n}-<date>", "pre-snapshot-<date>")
                    for row in s["preconditions"]
                ],
            ),
        )
    ],
)
def test_manifest_validator_pins_the_036_and_037_window_steps(
    name: str, mutate: Callable[[dict[str, Any], str], None]
) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    mutate(_window_step(manifest, name), name)
    with pytest.raises(wiring.WiringError, match="manifest_migrations_window"):
        wiring.validate_manifest(manifest)


def test_manifest_window_steps_say_their_own_upgrade_exactly_once() -> None:
    manifest = wiring.validate_manifest(wiring.load_yaml(MANIFEST))
    for name, window in mw.WINDOWS.items():
        step = _window_step(manifest, name)
        assert wiring._ALEMBIC_UPGRADE.findall(json.dumps(step)) == [name]
        assert f"`alembic upgrade {name}`" in step["change"]
        assert f"--window {name}" in step["change"]
        assert window.snapshot_pattern in json.dumps(step)
        assert "separate owner approval" in step["approval"]
    assert manifest["migrations_task_definition"]["windows"] == ["035", "036", "037"]


# ---------------------------------------------------------------------------
# T8 loop: window 037's dedicated read revision (--purpose read)
# ---------------------------------------------------------------------------


def test_the_037_read_revision_bakes_only_the_readback() -> None:
    status, rendered = _migrations(window=mw.WINDOW_037, purpose="read")
    assert status["blocks"] == [] and status["purpose"] == "read", status
    assert rendered is not None
    (container,) = rendered["containerDefinitions"]
    command = container["command"]
    assert command[:2] == ["python", "-c"]
    assert mw.normalized_read_command(command) == mw.normalized_read_command(
        ["python", "-c", mw.read_program(mw.WINDOW_037, mw.WINDOW_037.pre_read)]
    )
    for field in ("command", "entryPoint", "environment"):
        assert "alembic" not in json.dumps(container.get(field)).lower(), field
    _status, migrate = _migrations(window=mw.WINDOW_037, purpose="migrate")
    assert migrate is not None
    (migrate_container,) = migrate["containerDefinitions"]
    assert migrate_container["command"] == ["alembic", "upgrade", "037"]
    # Same family, image, roles, secret, logs and tags: only the command differs.
    assert {k: v for k, v in container.items() if k != "command"} == {
        k: v for k, v in migrate_container.items() if k != "command"
    }
    assert {k: v for k, v in rendered.items() if k != "containerDefinitions"} == {
        k: v for k, v in migrate.items() if k != "containerDefinitions"
    }


@pytest.mark.parametrize("name", ["035", "036"])
def test_only_window_037_has_a_read_revision(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status, rendered = _migrations(window=mw.WINDOWS[name], purpose="read")
    assert rendered is None
    assert f"migrations_read_revision_not_needed:{name}" in status["blocks"]
    snapshot = FakeAws(applied=True).snapshot(tmp_path / "readback.json")
    params = _write_params(tmp_path, app_image_override=OVERRIDE_IMAGE)
    code, out, err = _run(
        [
            "render-migrations-taskdef",
            "--window",
            name,
            "--purpose",
            "read",
            "--params",
            str(params),
            "--readback-file",
            str(snapshot),
        ],
        capsys,
    )
    assert code == 2 and out == ""
    assert f"migrations_read_revision_not_needed:{name}" in err


@pytest.mark.parametrize(
    "command",
    [
        ("alembic", "upgrade", "037"),
        (
            "python",
            "-c",
            mw.read_program(mw.WINDOW_037, mw.WINDOW_037.pre_read) + "# alembic",
        ),
        (
            "python",
            "-c",
            mw.read_program(mw.WINDOW_036, mw.WINDOW_036.pre_read),
        ),
        ("sh", "-c", "alembic upgrade 037"),
    ],
)
def test_the_read_purpose_refuses_any_other_command(command: tuple[str, ...]) -> None:
    with pytest.raises(wiring.WiringError, match="migrations_command_not_pinned"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=OVERRIDE_IMAGE,
            pins=_pins(),
            window=mw.WINDOW_037,
            purpose="read",
            command=command,
        )


def test_a_read_revision_never_carries_the_migration_tool_in_its_environment() -> None:
    fake = FakeAws(applied=True)
    _put_migrations(
        fake,
        _migrations_task_definition(
            environment=[{"name": "ALEMBIC_CONFIG", "value": "/app/alembic.ini"}]
        ),
    )
    status, rendered = _migrations(fake, window=mw.WINDOW_037, purpose="read")
    assert rendered is None
    assert "migrations_read_revision_names_the_migration_tool" in status["blocks"]


@pytest.mark.parametrize("purpose", ["", "both", "head", None])
def test_the_purpose_is_explicit_and_required(
    purpose: str | None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws(applied=True).snapshot(tmp_path / "readback.json")
    params = _write_params(tmp_path, app_image_override=OVERRIDE_IMAGE)
    argv = [
        "render-migrations-taskdef",
        "--window",
        "037",
        "--params",
        str(params),
        "--readback-file",
        str(snapshot),
    ]
    if purpose is not None:
        argv += ["--purpose", purpose]
    with pytest.raises(SystemExit) as raised:
        wiring.main(argv)
    assert raised.value.code == 2
    assert capsys.readouterr().out == ""
    with pytest.raises(wiring.WiringError, match="migrations_purpose_unknown"):
        wiring.render_migrations_task_definition(
            _migrations_task_definition(),
            image=OVERRIDE_IMAGE,
            pins=_pins(),
            window=mw.WINDOW_037,
            purpose=purpose or "",
        )


@pytest.mark.parametrize(
    "newest,target,expected",
    [
        ("read", "migrate", "would_register"),
        ("migrate", "read", "would_register"),
        ("read", "read", "registered"),
        ("migrate", "migrate", "registered"),
    ],
)
def test_the_037_read_and_migrate_revisions_are_registered_separately(
    newest: str, target: str, expected: str
) -> None:
    fake = FakeAws(applied=True)
    _status, rendered = _migrations(window=mw.WINDOW_037, purpose=newest)
    assert rendered is not None
    _put_migrations(
        fake, _registered_migrations(MIGRATIONS_NEXT_ARN, rendered), latest=True
    )
    status, again = _migrations(fake, window=mw.WINDOW_037, purpose=target)
    if expected == "registered":
        assert status["state"] == f"registered:{MIGRATIONS_NEXT_ARN}"
        assert status["run_task_definition"] == MIGRATIONS_NEXT_ARN
    else:
        assert status["state"] == "would_register", status
        assert status["newest_revision_purpose"] == newest
        assert status["run_task_definition"] is None
        assert again is not None


def test_a_read_revision_rendered_by_another_zlib_is_still_recognised() -> None:
    import base64
    import gzip

    fake = FakeAws(applied=True)
    _status, rendered = _migrations(window=mw.WINDOW_037, purpose="read")
    assert rendered is not None
    registered = _registered_migrations(MIGRATIONS_NEXT_ARN, rendered)
    container = registered["taskDefinition"]["containerDefinitions"][0]
    header = mw._READ_HEADER.match(container["command"][2])
    assert header is not None
    other = base64.b64encode(
        gzip.compress(mw.read_payload(mw.WINDOW_037.pre_read), compresslevel=1, mtime=0)
    ).decode()
    assert other != header.group("payload")
    container["command"][2] = container["command"][2].replace(
        header.group("payload"), other
    )
    _put_migrations(fake, registered, latest=True)
    status, _again = _migrations(fake, window=mw.WINDOW_037, purpose="read")
    assert status["state"] == f"registered:{MIGRATIONS_NEXT_ARN}"


@pytest.mark.parametrize("purpose", ["migrate", "read"])
def test_the_same_window_and_purpose_on_another_image_is_never_reused(
    purpose: str,
) -> None:
    """P03: the newest revision is this window's and purpose's exact rendering
    but on another image: a new registration would silently fork the window,
    so it blocks for review instead."""

    window = mw.WINDOW_037 if purpose == "read" else mw.WINDOW_036
    other_image = OVERRIDE_IMAGE.replace("sha256:" + "2" * 64, "sha256:" + "5" * 64)
    assert other_image != OVERRIDE_IMAGE
    rendered = wiring.render_migrations_task_definition(
        _migrations_task_definition(),
        image=other_image,
        pins=_pins(),
        window=window,
        purpose=purpose,
    )
    fake = FakeAws(applied=True)
    _put_migrations(
        fake, _registered_migrations(MIGRATIONS_NEXT_ARN, rendered), latest=True
    )
    status, again = _migrations(fake, window=window, purpose=purpose)
    assert again is None
    assert status["run_task_definition"] is None
    assert (
        f"migrations_newer_revision_differs:{MIGRATIONS_NEXT_ARN}" in status["blocks"]
    )


# ---------------------------------------------------------------------------
# E7-B0: the outbox worker flag is a reviewed parameter ('true' since E7-B1)
# ---------------------------------------------------------------------------

WORKER_FLAG = "BILLING_FACT_WORKER_ENABLED"
REVISION_10_ARN = SOURCE_ARN.rsplit(":", 1)[0] + ":10"
# Every rollback target the manifest may hold, set explicitly: these tests do
# not depend on which one the reviewed manifest carries. Revision 9 names its
# pending re-pin to 10 (before the image bump is deployed).
WORKER_ROLLBACK_TARGETS: dict[int, dict[str, Any]] = {
    8: {
        "revision": 8,
        "effective_after": wiring.ROLLBACK_EFFECTIVE_INITIAL,
        "repin": {
            "revision": 9,
            "effective_after": wiring.ROLLBACK_EFFECTIVE_BRIDGE,
            "before": "migration_036",
            "why": "Revision 8 reads legacy columns that migration 036 drops.",
        },
    },
    9: {
        "revision": 9,
        "effective_after": wiring.ROLLBACK_EFFECTIVE_BRIDGE,
        "repin": {
            "revision": 10,
            "effective_after": wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG,
            "before": "app_image_bump_deployment",
            "why": (
                "Revision 9 carries BILLING_FACT_WORKER_ENABLED 'false': a"
                " rollback to it from the image bump would turn the worker off."
            ),
        },
    },
    10: {"revision": 10, "effective_after": wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG},
}
# stdout of render-taskdef and check BEFORE the flag became a parameter (the
# literal 'false', main a42754cd), in the D3 world (service on 8) and in the
# bridge world (service on 9 registered from the rendering on the bridge image).
GOLDEN_RENDER_SHA256 = {
    "d3": "7cd9343411cbaf133f9586dbb6bbae6eca850b42399dfe4cdea5011aa12769d5",
    "bridge": "ee061bb407528bb48551f22309781528daaea9ff246aabb98a17f6ef194bd784",
}
GOLDEN_CHECK_SHA256 = {
    "d3": "7092b6e117cc91a85ff6fb1b103cbdaff228dadae71f1c966525653056bb9d47",
    "bridge": "3e6486e0fc5e00c126fb575c39fcd3049a5e85b3c0000ef9d8226994e2f0d617",
}
WORKER_WORLDS = [("d3", 8), ("bridge", 8), ("bridge", 9)]


def _worker_manifest(rollback: int) -> dict[str, Any]:
    manifest: dict[str, Any] = wiring.load_yaml(MANIFEST)
    manifest["rollback_target"] = copy.deepcopy(WORKER_ROLLBACK_TARGETS[rollback])
    return manifest


def _worker_pins(rollback: int) -> Any:
    return _pins(_worker_manifest(rollback))


def _registered_as(arn: str, rendered: dict[str, Any]) -> dict[str, Any]:
    """DescribeTaskDefinition of ``arn`` as registered from ``rendered``."""

    registered = _task_definition(arn)
    registered["taskDefinition"]["containerDefinitions"] = copy.deepcopy(
        rendered["containerDefinitions"]
    )
    registered["tags"] = copy.deepcopy(rendered["tags"])
    return registered


def _worker_rendering(flag: str, **changes: Any) -> dict[str, Any]:
    """The rendering of the D3 world (service on 8) with the flag ``flag``."""

    _plan_result, rendered = _plan(
        FakeAws(applied=True), pins=_worker_pins(8), **{WORKER_FLAG: flag}, **changes
    )
    assert rendered is not None
    return rendered


def _put_app_revision(
    fake: FakeAws, arn: str, response: dict[str, Any], *, latest: bool
) -> None:
    revision = arn.rsplit(":", 1)[1]
    fake.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": f"neurostack-staging-app:{revision}", "include": ["TAGS"]},
        response,
    )
    if latest:
        fake.put(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-app", "include": ["TAGS"]},
            response,
        )


def _serve(fake: FakeAws, arn: str) -> None:
    fake.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(arn),
    )


def _bridge_world(flag: str = "false") -> FakeAws:
    """Live today: the service runs revision 9, the rendering on the bridge
    image (app_image_override) with the worker flag ``flag``."""

    fake = FakeAws(applied=True)
    rendered = _worker_rendering(flag, app_image_override=OVERRIDE_IMAGE)
    _put_app_revision(fake, NEXT_ARN, _registered_as(NEXT_ARN, rendered), latest=True)
    _serve(fake, NEXT_ARN)
    return fake


def _bridge_plan(
    flag: str, *, rollback: int = 8, fake: FakeAws | None = None
) -> tuple[dict[str, Any], Any]:
    return _plan(
        fake if fake is not None else _bridge_world(),
        pins=_worker_pins(rollback),
        app_image_override=OVERRIDE_IMAGE,
        **{WORKER_FLAG: flag},
    )


def _worker_cli(
    command: str,
    world: str,
    rollback: int,
    flag: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    fake = FakeAws(applied=True) if world == "d3" else _bridge_world()
    directory = tmp_path / f"{command}-{world}-{rollback}-{flag}"
    directory.mkdir()
    manifest = directory / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(_worker_manifest(rollback)), encoding="utf-8")
    raw = _parameters(**{WORKER_FLAG: flag})
    if world == "bridge":
        raw["parameters"]["app_image_override"] = OVERRIDE_IMAGE
    params = directory / "params.yaml"
    params.write_text(yaml.safe_dump(raw), encoding="utf-8")
    snapshot = fake.snapshot(directory / "readback.json")
    return _run(
        [
            command,
            "--params",
            str(params),
            "--readback-file",
            str(snapshot),
            "--manifest",
            str(manifest),
        ],
        capsys,
    )


def test_the_repository_turns_the_outbox_worker_on() -> None:
    """The repository value is 'true', set deliberately by a reviewed PR.

    E7-B1, the committed, reviewed PR that turned the repository parameter on
    (owner decision 2026-10-22: the flag first, on the bridge image, then the
    E7 image in a later revision of its own), changed the parameters line and
    the tests that pin it together: this one (the parameter and its
    rendering), test_the_docs_state_that_the_repository_turns_the_worker_on
    (the docs) and test_the_repository_parameters_propose_the_next_revision
    (the planner on the repository file). Turning it off again is the same
    kind of PR, back to 'false', and changes those tests with it
    (docs/runbooks/asaas-deployment.md, "The outbox worker flag").
    """

    text = PARAMS.read_text(encoding="utf-8")
    assert "\n  BILLING_FACT_WORKER_ENABLED: 'true'\n" in text
    assert "\n  BILLING_FACT_WORKER_ENABLED: 'false'\n" not in text
    assert wiring.validate_parameters(wiring.load_yaml(PARAMS))[WORKER_FLAG] == "true"
    _manifest, parameters, _digests = wiring.load_inputs(MANIFEST, PARAMS)
    assert parameters is not None and parameters[WORKER_FLAG] == "true"
    _plan_result, rendered = _plan(FakeAws(applied=True), pins=_worker_pins(8))
    assert rendered is not None
    app = evidence._container_map(rendered)["app"]
    assert evidence._env_map(app)[WORKER_FLAG] == "true"
    manifest = wiring.load_yaml(MANIFEST)
    assert manifest["task_definition"]["app_environment_added"][WORKER_FLAG] == {
        "parameter": WORKER_FLAG
    }
    assert manifest["parameters"]["required"][WORKER_FLAG] == {
        "validator": "exact_false_or_true",
        "path": None,
    }


WORKER_FLAG_RUNBOOK = ROOT / "docs/runbooks/asaas-deployment.md"
WORKER_FLAG_CONFIGURATION = ROOT / "docs/configuration.md"
WORKER_FLAG_ROLLBACK = ROOT / "docs/runbooks/asaas-rollback.md"
# The runbook's state section (operator-recorded live evidence); the same
# heading as STATE_HEADING in test_production_migration_window.py.
RUNBOOK_STATE_HEADING = "## State after the live runs of 2026-10-22 to 2026-10-25\n"
# Statements of the repository value from before E7-B1; none may come back
# while the repository says 'true'.
RETIRED_WORKER_FLAG_STATEMENTS = (
    # Untrue since revision 10 ('true') was deployed on 2026-10-25.
    "the worker is not on in production",
    "steps 2 to 6 are not done",
    "keeps_the_outbox_worker_off",
    "The repository keeps `'false'`",
    "the repository keeps 'false'",
    "The repository keeps 'false'",
    'The repository keeps "false"',
    "`'false'` (the repository value)",
    "`'false'` in the repository",
    "'false' in the repository",
    "`false` for revision 9 and in the repository",
    "`'false'` (o valor do repositório)",
    "`'false'` no repositório",
    "the only test that pins the value",
    "pinned by one test",
    # Untrue since the worker runs: its maintenance pass calls the reconciler
    # every 30 s (only the publisher has no runtime caller).
    "and reconciler have no runtime caller",
)
# The worker is on in production since revision 10 (deployment completed
# 2026-10-25 01:51Z, operator-recorded) and revision 11 carries the flag too.
# The docs must say so where the live state is recorded...
WORKER_ON_IN_PRODUCTION_FACTS = {
    "runbook state section": (
        "The worker is on in production since revision 10 (2026-10-25 01:51Z),"
        " and revision 11 carries the flag too."
    ),
    "configuration.md": "**O worker está ligado em produção desde a revisão 10**",
}
# ...and no statement that it is off, or not yet on a registered revision,
# may come back (compared lowercased).
WORKER_OFF_IN_PRODUCTION_CLAIMS = (
    "the worker is not yet on",
    "the worker is still off",
    "nothing here claims the flag is on",
    "nothing here claims the worker is on",
    "no registered revision carries `true` yet",
    "o worker ainda não está ligado",
    "ainda em nenhuma revisão registrada",
)
# The same claims with words in between ("the outbox worker is still off in
# production", "a flag do worker segue desligada em produção"): the worker or
# the flag, then off, then production, within one sentence.
_OFF_IN_PRODUCTION = re.compile(
    r"\b(?:worker|flag)\b[^.;\n]{0,40}"
    r"\b(?:off|not on|not enabled|not yet on|disabled|desligad[oa]"
    r"|não está ligad[oa]|não ligad[oa]|ainda não)\b"
    r"[^.;\n]{0,40}\b(?:in production|em produção)",
    re.IGNORECASE,
)


def _worker_off_claims(text: str) -> list[str]:
    lowered = text.lower()
    found = [claim for claim in WORKER_OFF_IN_PRODUCTION_CLAIMS if claim in lowered]
    found.extend(match.group(0) for match in _OFF_IN_PRODUCTION.finditer(text))
    return found


def _runbook_state_section() -> str:
    raw = WORKER_FLAG_RUNBOOK.read_text(encoding="utf-8")
    start = raw.index(RUNBOOK_STATE_HEADING)
    end = raw.index("\n## ", start + len(RUNBOOK_STATE_HEADING))
    return raw[start:end]


@pytest.mark.parametrize(
    ("wording", "claims_off"),
    [
        pytest.param("The worker is still off in production.", True, id="plain"),
        pytest.param(
            "The outbox worker is still off in production.", True, id="interposed"
        ),
        pytest.param(
            "The billing fact worker is not enabled in production yet.",
            True,
            id="not-enabled",
        ),
        pytest.param(
            "The worker flag is disabled in production.", True, id="flag-disabled"
        ),
        pytest.param(
            "o worker do outbox está desligado em produção.", True, id="pt-off"
        ),
        pytest.param(
            "A flag do worker ainda não está ligada em produção.",
            True,
            id="pt-not-yet",
        ),
        pytest.param(
            "No registered revision carries `true` yet.", True, id="not-registered"
        ),
        # The true statements and the procedure wording stay allowed.
        pytest.param(
            "The worker is on in production since revision 10 (2026-10-25"
            " 01:51Z), and revision 11 carries the flag too.",
            False,
            id="true-en",
        ),
        pytest.param(
            "**O worker está ligado em produção desde a revisão 10**",
            False,
            id="true-pt",
        ),
        pytest.param(
            "`worker_flag: {from: 'true', to: 'false'}` (turning the worker off"
            " again, a new revision).",
            False,
            id="turn-off-procedure",
        ),
        pytest.param(
            "A rollback to revision 9 would also turn the worker off. Revision 11"
            " serves in production now.",
            False,
            id="rollback-consequence",
        ),
    ],
)
def test_the_worker_off_scan_catches_variants_and_spares_the_truth(
    wording: str, claims_off: bool
) -> None:
    assert bool(_worker_off_claims(" ".join(wording.split()))) is claims_off


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin + len(start))]


def _in_order(text: str, steps: list[str]) -> None:
    positions = [text.index(step) for step in steps]
    assert positions == sorted(positions), steps


def test_the_docs_state_that_the_repository_turns_the_worker_on() -> None:
    """The docs follow the pin and the live state: the repository says 'true',
    revision 10 with 'true' was registered and deployed on 2026-10-25, the
    worker is on in production since then (revision 11 keeps the flag), and
    the steps are in order. The PR that turns the worker off again changes
    this test with the pin test."""

    assert wiring.validate_parameters(wiring.load_yaml(PARAMS))[WORKER_FLAG] == "true"
    for path in (
        WORKER_FLAG_RUNBOOK,
        WORKER_FLAG_CONFIGURATION,
        WORKER_FLAG_ROLLBACK,
        PARAMS,
        MANIFEST,
        ROOT / "scripts/asaas_production_wiring.py",
    ):
        text = _flat(path)
        for retired in RETIRED_WORKER_FLAG_STATEMENTS:
            assert retired not in text, (path.name, retired)
        assert _worker_off_claims(text) == [], path.name

    state = " ".join(_runbook_state_section().split())
    assert WORKER_ON_IN_PRODUCTION_FACTS["runbook state section"] in state
    assert WORKER_ON_IN_PRODUCTION_FACTS["configuration.md"] in _flat(
        WORKER_FLAG_CONFIGURATION
    )
    # On, but still publishing nothing: no transport, no consumer. The
    # reconciler does run (src/services/billing_fact_worker.py, _maintain).
    assert (
        "**Outbox publication is not scheduled yet.** This still holds with"
        " `BILLING_FACT_WORKER_ENABLED=true` (revisions 10 and 11): the worker"
        " runs with `transport=None` and logs `consumer=absent`, so it publishes"
        " nothing. The publisher has no runtime caller; it runs only with a"
        " transport. The reconciler does run, in the worker's maintenance pass"
        " every 30 s. Facts stay `pending` with no attempt counted, and a fact"
        " still unacknowledged after 900 s gets an `ack overdue` alert row and"
        " the WARN `billing fact acknowledgement overdue`. Nothing in ECS"
        " evaluates the alert rules" in _flat(WORKER_FLAG_RUNBOOK)
    )

    runbook = _flat(WORKER_FLAG_RUNBOOK)
    section = _between(
        runbook,
        "### The outbox worker flag (`BILLING_FACT_WORKER_ENABLED`)",
        "### Ordered procedure",
    )
    for phrase in (
        "**The repository says `'true'` since E7-B1**, the reviewed PR that"
        " turned the repository parameter on deliberately.",
        "`test_the_repository_turns_the_outbox_worker_on`",
        "**Revision 10, with the flag `true`, was registered and deployed on"
        " 2026-10-25** (deployment completed 01:51Z, 8 `billing fact worker"
        " started` lines, no tick failure)",
        "ECS assigns the number at registration (expected `:10`)",
        # The result while revision 10 served, and since revision 11 serves.
        "While revision 10 served (2026-10-25 01:51Z to 15:04Z), against the"
        " live state (revision 10 serving) with the override set to the bridge"
        " digest, `'true'` reports `status: compliant` with"
        " `registered_and_deployed:<revision 10 ARN>`, and `'false'` reports"
        " `status: ready` with one change, `replaces: <revision 10 ARN>` and"
        " `worker_flag: {from: 'true', to: 'false'}`",
        "Since revision 11 serves, the override that matches the live state is"
        " the E7b digest, and `plan` reports"
        " `registered_and_deployed:<revision 11 ARN>`",
        # The override's value today, and the bridge wording as history.
        "Today that is the E7b digest revision 11 runs; at the time of E7-B1 it"
        " was the bridge digest revision 9 ran, and the bridge wording in this"
        " procedure is historical.",
        "Steps 2 to 4 were done on 2026-10-25: revision 10, with the flag"
        " `true`, was registered and deployed (deployment completed 01:51Z, 8"
        " `billing fact worker started` lines, no tick failure).",
        "The image does not change in this revision, and the E7 image is never"
        " part of it: the E7 image comes in a later revision of its own",
        "Recovery is an owner decision, carried out by the operator and never by"
        " these tools",
        # What a pass reads and writes (src/services/billing_fact_worker.py).
        "`billing_payment_effects` (provider `asaas`), `billing_outbox`,"
        " `billing_fact_deliveries`, `billing_fact_handoffs`, `billing_fact_acks`"
        " and `billing_fact_reconciliation_alerts`. It can write:",
        "With those tables empty it writes nothing.",
        "so up to 8 passes run per 30 s",
        "`BillingFactDeliveryStale` does not fire there",
    ):
        assert phrase in section, phrase
    assert "billing_outbox_events" not in section
    _in_order(
        section,
        [
            "1. **PR.**",
            "2. **Plan, render, check**",
            "proposes the next revision (expected `:10`), flag `false` to `true`,"
            " on the same bridge image",
            "3. **Register** the rendering as revision 10 (`ManagedBy=manual`",
            "4. **Deploy** revision 10: the `service_deployment` gate (separate"
            " owner approval)",
            "5. **Observe**",
            "8 lines from the revision 10 tasks, 4 per task × 2 tasks, not one per"
            " task",
            "--log-group-name /ecs/neurostack-staging/app",
            "--filter-pattern '\"billing fact worker started\"'",
            "`'\"billing fact acknowledgement overdue\"'` (the WARN) returns no"
            " event",
            "`neurostack_billing_fact_worker_last_tick_timestamp_seconds` keeps"
            " advancing",
            "a payment effect with no outbox row does not show in it",
            "FROM billing_payment_effects WHERE provider = 'asaas'",
            "FROM billing_outbox)",
            "FROM billing_fact_deliveries)",
            "The counts are the same before and after.",
            "6. **Regenerate the protected template from revision 10** before the"
            " E7 image",
        ],
    )

    # E7-A follow-up: the flag first, observed, then the E7 image. Each step
    # records its operator-recorded completion (revision 10 carries 'true'
    # since 2026-10-25 01:51Z, its observation closed at 12:42Z, revision 11
    # serves since 15:04Z); the pre-deploy wording is retired.
    follow_up = _between(
        runbook, "1. **Worker flag first, its own change.**", "4. **Receiver"
    )
    flag_step = _between(
        follow_up, "1. **Worker flag first", "2. **Observe the worker.**"
    )
    assert "no registered revision carries `true` yet" not in flag_step
    assert "revision 10 was the first to carry `true`" in flag_step
    assert (
        "Done: revision 10 has carried `true` since its deployment completed on"
        " 2026-10-25 01:51Z (operator record)." in flag_step
    )
    assert (
        "Done: the observation of revision 10 closed at 2026-10-25T12:42Z" in follow_up
    )
    assert (
        "Done: revision 11, on the E7b image, has served since 2026-10-25 15:04Z"
        in follow_up
    )
    _in_order(
        follow_up,
        [
            "1. **Worker flag first, its own change.** The worker code is already"
            " in the bridge image, so the flag is turned on first, on the bridge"
            " image, as its own reviewed change and its own deploy",
            "2. **Observe the worker.** Before any E7 build:",
            "`billing fact worker started` (8 lines, 4 per task × 2 tasks)",
            "3. **E7 image after, never in the same revision.**",
            "the flag change and the E7 image never share a revision or a deploy",
        ],
    )

    configuration = _flat(WORKER_FLAG_CONFIGURATION)
    section = _between(
        configuration, "## Worker do outbox `billing.fact.v1`", "| Variável |"
    )
    for phrase in (
        "`'true'` (o valor do repositório desde o PR revisado E7-B1, que ligou o"
        " parâmetro do repositório)",
        WORKER_ON_IN_PRODUCTION_FACTS["configuration.md"],
        "(deploy concluído em 2026-10-25 01:51Z, evidência registrada pelo"
        " operador), e a revisão 11, no ar desde 2026-10-25 15:04Z, também leva"
        " `true`; as revisões 8 e 9 levam `false`.",
        "Todos os passos abaixo foram feitos em 2026-10-25; ficam descritos como"
        " procedimento.",
        "`test_the_repository_turns_the_outbox_worker_on`",
    ):
        assert phrase in section, phrase
    _in_order(
        section,
        [
            "propõe a próxima revisão (esperada `:10`; o número é atribuído pelo"
            " ECS), flag `false` para `true`, na imagem ponte",
            "o operador a registra (`ManagedBy=manual`)",
            "o deploy tem aprovação própria",
            "8 linhas no log group `/ecs/neurostack-staging/app`, 4 por task × 2"
            " tasks",
            "`neurostack_billing_fact_worker_last_tick_timestamp_seconds` avança,"
            " mas o `/metrics` é por processo",
            "nenhum WARN `billing fact acknowledgement overdue`",
            "`billing_payment_effects` com `provider = 'asaas'`",
            "iguais antes e depois do deploy",
            "regenerar o template protegido a partir dessa revisão antes da imagem"
            " do E7",
        ],
    )
    order = _between(configuration, "Ordem do E7 na AWS", "## Primeiro acesso")
    flag_step = _between(
        order, "1. **Primeiro a flag, numa mudança própria.**", "2. **Observar.**"
    )
    assert "a revisão 10 foi a primeira a levá-lo" in flag_step
    assert (
        "Feito: a revisão 10 leva `true` desde o fim do seu deploy, em"
        " 2026-10-25 01:51Z (evidência do operador)." in flag_step
    )
    assert "Feito: a observação da revisão 10 fechou em 2026-10-25T12:42Z" in order
    assert (
        "Feito: a revisão 11, na imagem E7b, está no ar desde 2026-10-25 15:04Z"
        in order
    )
    _in_order(
        order,
        [
            "1. **Primeiro a flag, numa mudança própria.** O código do worker já"
            " está na imagem ponte, então `BILLING_FACT_WORKER_ENABLED=true` sai"
            " antes, na imagem ponte",
            "2. **Observar.**",
            "3. **Depois, a imagem do E7, nunca na mesma revisão.**",
        ],
    )


def _repository_params_copy(directory: Path, flag: str | None) -> Path:
    """The operator's local copy of the repository file: app_image_override set
    to the bridge digest. ``flag=None`` keeps the repository's flag line as it
    is; a value rewrites it (the control run only)."""

    text = PARAMS.read_text(encoding="utf-8")
    image = re.compile(r"^  app_image_override: null$", re.MULTILINE)
    line = re.compile(rf"^  {WORKER_FLAG}: '(?:false|true)'$", re.MULTILINE)
    assert len(image.findall(text)) == 1 and len(line.findall(text)) == 1
    text = image.sub(f"  app_image_override: {OVERRIDE_IMAGE}", text)
    if flag is not None:
        text = line.sub(f"  {WORKER_FLAG}: '{flag}'", text)
    path = directory / f"params-{flag or 'repository'}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_repository_parameters_propose_the_next_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository file as it is (only the apply-time image set), against
    the fixture of the bridge state before revision 10 was registered
    (revision 9 serving 'false') and the manifest of that state (rollback
    target 9, its re-pin to 10 pending): plan proposes one new revision that
    flips the flag alone, render-taskdef differs from the 'false' control
    rendering in that one value, and check passes. Fails when the repository
    value is not 'true'. The image bump from revision 10 is
    test_the_repository_parameters_propose_the_image_bump."""

    snapshot = _bridge_world().snapshot(tmp_path / "readback.json")
    manifest = tmp_path / "manifest-bridge.yaml"
    manifest.write_text(yaml.safe_dump(_worker_manifest(9)), encoding="utf-8")
    outputs: dict[str, dict[str, str]] = {}
    for flag in ("true", "false"):
        # The 'true' run reads the repository's own flag line; only the
        # 'false' control run rewrites it.
        params = _repository_params_copy(tmp_path, None if flag == "true" else flag)
        outputs[flag] = {}
        for command in ("plan", "render-taskdef", "check"):
            code, out, err = _run(
                [
                    command,
                    "--params",
                    str(params),
                    "--readback-file",
                    str(snapshot),
                    "--manifest",
                    str(manifest),
                ],
                capsys,
            )
            assert code == 0, (flag, command, err)
            outputs[flag][command] = out

    plan = json.loads(outputs["true"]["plan"])
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "ready"
    assert plan["parameters"][WORKER_FLAG] == "true"
    assert step["state"] == "would_change" and step["blocks"] == []
    [change] = step["changes"]
    assert change["action"] == "register_task_definition"
    assert change["replaces"] == NEXT_ARN
    assert change["worker_flag"] == {"name": WORKER_FLAG, "from": "false", "to": "true"}
    assert change["request_sha256"] == wiring._sha256(
        json.loads(outputs["true"]["render-taskdef"])
    )
    assert json.loads(outputs["false"]["plan"])["status"] == "compliant"

    off, on = outputs["false"]["render-taskdef"], outputs["true"]["render-taskdef"]
    changed = [
        (before.strip(), after.strip())
        for before, after in zip(off.splitlines(), on.splitlines(), strict=True)
        if before != after
    ]
    assert changed == [('"value": "false"', '"value": "true"')]
    app = evidence._container_map(json.loads(on))["app"]
    assert evidence._env_map(app)[WORKER_FLAG] == "true"
    assert (
        json.loads(outputs["true"]["check"])["rendered_request_sha256"]
        != json.loads(outputs["false"]["check"])["rendered_request_sha256"]
    )


@pytest.mark.parametrize("value", ["false", "true"])
def test_the_worker_flag_parameter_is_exactly_false_or_true(value: str) -> None:
    parameters = _validated(**{WORKER_FLAG: value})
    assert parameters[WORKER_FLAG] == value
    assert type(parameters[WORKER_FLAG]) is str


@pytest.mark.parametrize(
    "value,reason",
    [
        (None, "parameter_missing"),
        ("", "parameter_missing"),
        ("   ", "parameter_missing"),
        ("yes", "parameter_invalid"),
        ("no", "parameter_invalid"),
        ("on", "parameter_invalid"),
        ("True", "parameter_invalid"),
        ("TRUE", "parameter_invalid"),
        ("False", "parameter_invalid"),
        ("1", "parameter_invalid"),
        ("0", "parameter_invalid"),
        ("enabled", "parameter_invalid"),
        (" false", "parameter_invalid"),
        ("false ", "parameter_invalid"),
        ("true\n", "parameter_invalid"),
        ("\ttrue", "parameter_invalid"),
        ("'true'", "parameter_invalid"),
        # YAML booleans and numbers: the reviewed value is the string itself.
        (True, "parameter_invalid"),
        (False, "parameter_invalid"),
        (1, "parameter_invalid"),
        (0, "parameter_invalid"),
        (1.0, "parameter_invalid"),
        (["true"], "parameter_invalid"),
        ({"value": "true"}, "parameter_invalid"),
    ],
)
def test_the_worker_flag_parameter_refuses_anything_else(
    value: Any, reason: str
) -> None:
    with pytest.raises(wiring.WiringError, match=f"^{reason}:{WORKER_FLAG}$"):
        _validated(**{WORKER_FLAG: value})


def test_a_missing_worker_flag_parameter_fails_the_plan() -> None:
    raw = _parameters()
    del raw["parameters"][WORKER_FLAG]
    with pytest.raises(wiring.WiringError, match=f"^parameter_missing:{WORKER_FLAG}$"):
        wiring.validate_parameters(raw)


@pytest.mark.parametrize("spelling", ["true", "false", "yes", "on", "True", "1"])
def test_an_unquoted_yaml_flag_fails_before_any_aws_read(
    spelling: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = PARAMS.read_text(encoding="utf-8")
    line = re.compile(rf"^  {WORKER_FLAG}: '(?:false|true)'$", re.MULTILINE)
    assert len(line.findall(text)) == 1
    params = tmp_path / "params.yaml"
    params.write_text(
        line.sub(f"  {WORKER_FLAG}: {spelling}", text),
        encoding="utf-8",
    )
    fake = FakeAws(applied=True)
    for command in ("plan", "render-taskdef", "check", "check-manifest"):
        code = wiring.main(
            [command, "--params", str(params)], transport_factory=lambda: fake.transport
        )
        captured = capsys.readouterr()
        assert code == 2
        assert captured.out == ""
        assert f"blocked: parameter_invalid:{WORKER_FLAG}" in captured.err
    assert fake.calls == []


@pytest.mark.parametrize("world,rollback", WORKER_WORLDS)
def test_false_renders_and_checks_byte_for_byte_as_before(
    world: str, rollback: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for command, golden in (
        ("render-taskdef", GOLDEN_RENDER_SHA256),
        ("check", GOLDEN_CHECK_SHA256),
    ):
        code, out, err = _worker_cli(
            command, world, rollback, "false", tmp_path, capsys
        )
        assert code == 0, err
        assert wiring.hashlib.sha256(out.encode("utf-8")).hexdigest() == golden[world]


@pytest.mark.parametrize("world,rollback", WORKER_WORLDS)
def test_true_renders_the_same_request_but_for_the_one_value(
    world: str, rollback: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, off, err = _worker_cli(
        "render-taskdef", world, rollback, "false", tmp_path, capsys
    )
    assert code == 0, err
    code, on, err = _worker_cli(
        "render-taskdef", world, rollback, "true", tmp_path, capsys
    )
    assert code == 0, err
    changed = [
        (before, after)
        for before, after in zip(off.splitlines(), on.splitlines(), strict=True)
        if before != after
    ]
    assert [(a.strip(), b.strip()) for a, b in changed] == [
        ('"value": "false"', '"value": "true"')
    ]
    request_off, request_on = json.loads(off), json.loads(on)
    environment = evidence._container_map(request_on)["app"]["environment"]
    # Same position as before: after the source names, the D3 names sorted.
    assert [row["name"] for row in environment][-4:] == sorted(wiring.ADDED_ENVIRONMENT)
    assert environment[-1] == {"name": WORKER_FLAG, "value": "true"}
    environment[-1]["value"] = "false"
    assert request_on == request_off


@pytest.mark.parametrize("rollback", [8, 9])
def test_plan_keeps_revision_9_compliant_while_the_flag_is_false(
    rollback: int,
) -> None:
    plan, rendered = _bridge_plan("false", rollback=rollback)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "compliant"
    assert step["state"] == "compliant"
    assert f"registered_and_deployed:{NEXT_ARN}" in step["compliant"]
    assert step["changes"] == [] and step["blocks"] == []
    assert plan["would_change"] == []
    assert plan["parameters"][WORKER_FLAG] == "false"
    assert evidence._env_map(evidence._container_map(rendered)["app"])[WORKER_FLAG] == (
        "false"
    )


@pytest.mark.parametrize("rollback", [8, 9])
def test_plan_with_true_on_revision_9_reports_a_new_revision(rollback: int) -> None:
    plan, rendered = _bridge_plan("true", rollback=rollback)
    assert rendered is not None
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "ready"
    assert step["state"] == "would_change"
    assert step["blocks"] == []
    assert plan["would_change"] == ["task_definition_revision:register_task_definition"]
    assert step["changes"] == [
        {
            "action": "register_task_definition",
            "family": "neurostack-staging-app",
            "derived_from": SOURCE_ARN,
            "replaces": NEXT_ARN,
            "worker_flag": {"name": WORKER_FLAG, "from": "false", "to": "true"},
            "request_sha256": wiring._sha256(rendered),
            "service_changes": "none (service_deployment is a separate gate)",
        }
    ]
    assert plan["parameters"][WORKER_FLAG] == "true"
    assert wiring.renderable(plan, rendered) is rendered
    assert evidence._env_map(evidence._container_map(rendered)["app"])[WORKER_FLAG] == (
        "true"
    )


@pytest.mark.parametrize("rollback", [8, 9])
def test_a_registered_true_revision_is_reported_until_and_after_it_serves(
    rollback: int,
) -> None:
    fake = _bridge_world("false")
    _plan_result, rendered = _bridge_plan("true", rollback=rollback, fake=fake)
    _put_app_revision(
        fake, REVISION_10_ARN, _registered_as(REVISION_10_ARN, rendered), latest=True
    )
    fake.calls.clear()
    plan, _rendered = _bridge_plan("true", rollback=rollback, fake=fake)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "compliant", plan["blocks"]
    assert f"registered_not_deployed:{REVISION_10_ARN}" in step["compliant"]
    assert step["info"]["registered_revision_changes_worker_flag"] == {
        "serving": NEXT_ARN,
        "from": "false",
        "to": "true",
    }
    # The serving revision 9 is read exactly once (as the rollback target, or
    # as the serving revision when the rollback target is 8).
    nine = {"taskDefinition": "neurostack-staging-app:9", "include": ["TAGS"]}
    assert [call[2] for call in fake.calls].count(nine) == 1
    _serve(fake, REVISION_10_ARN)
    plan, _rendered = _bridge_plan("true", rollback=rollback, fake=fake)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "compliant"
    assert f"registered_and_deployed:{REVISION_10_ARN}" in step["compliant"]
    # Turning the worker off again is also a reviewed new revision.
    plan, rendered_off = _bridge_plan("false", rollback=rollback, fake=fake)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "ready"
    assert [change["worker_flag"] for change in step["changes"]] == [
        {"name": WORKER_FLAG, "from": "true", "to": "false"}
    ]
    assert step["changes"][0]["replaces"] == REVISION_10_ARN
    assert evidence._env_map(evidence._container_map(rendered_off)["app"])[
        WORKER_FLAG
    ] == ("false")


REVISION_11_ARN = SOURCE_ARN.rsplit(":", 1)[0] + ":11"


def _stuck_world(
    serving: str, serving_flag: str, newest: str, newest_flag: str
) -> FakeAws:
    """The service on ``serving``; the newest registered revision is ``newest``;
    revision 9 (the rollback target) is the 'false' bridge rendering."""

    fake = FakeAws(applied=True)
    for arn, flag, latest in (
        (NEXT_ARN, "false", False),
        (serving, serving_flag, False),
        (newest, newest_flag, True),
    ):
        rendered = _worker_rendering(flag, app_image_override=OVERRIDE_IMAGE)
        _put_app_revision(fake, arn, _registered_as(arn, rendered), latest=latest)
    _serve(fake, serving)
    return fake


# (serving, its flag, newest registered, its flag, parameter): the flag
# revision abandoned or rolled back with the parameter back to 'false', and
# the repository's 'false' rendering registered after the worker was on.
STUCK_STATES = [
    (NEXT_ARN, "false", REVISION_10_ARN, "true", "false"),
    (REVISION_10_ARN, "true", REVISION_11_ARN, "false", "true"),
]


@pytest.mark.parametrize("rollback", [8, 9])
@pytest.mark.parametrize("serving,serving_flag,newest,newest_flag,flag", STUCK_STATES)
def test_a_newest_revision_differing_only_in_the_flag_blocks_under_its_own_name(
    serving: str,
    serving_flag: str,
    newest: str,
    newest_flag: str,
    flag: str,
    rollback: int,
) -> None:
    fake = _stuck_world(serving, serving_flag, newest, newest_flag)
    plan, _rendered = _bridge_plan(flag, rollback=rollback, fake=fake)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert step["state"] == "blocked"
    assert step["blocks"] == [f"newest_revision_differs_only_in_worker_flag:{newest}"]
    assert step["changes"] == []
    assert not any(item.startswith("registered_") for item in step["compliant"])
    assert step["info"]["newest_revision_worker_flag"] == {
        "newest": newest,
        "newest_value": newest_flag,
        "rendered_value": flag,
        "serving": serving,
    }
    # Facts only: the planner never performs or names a deregistration.
    assert "deregister" not in json.dumps(plan).lower()


def test_render_taskdef_refuses_the_stuck_state_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _stuck_world(NEXT_ARN, "false", REVISION_10_ARN, "true")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(_worker_manifest(9)), encoding="utf-8")
    raw = _parameters(**{WORKER_FLAG: "false"})
    raw["parameters"]["app_image_override"] = OVERRIDE_IMAGE
    params = tmp_path / "params.yaml"
    params.write_text(yaml.safe_dump(raw), encoding="utf-8")
    args = [
        "--params",
        str(params),
        "--readback-file",
        str(fake.snapshot(tmp_path / "readback.json")),
        "--manifest",
        str(manifest),
    ]
    for command in ("render-taskdef", "check"):
        code, out, err = _run([command, *args], capsys)
        assert code == 2 and out == ""
        assert (
            "blocked: plan_blocked:"
            f"newest_revision_differs_only_in_worker_flag:{REVISION_10_ARN}" in err
        )


def test_a_drifted_serving_revision_keeps_the_generic_block() -> None:
    fake = _stuck_world(NEXT_ARN, "false", REVISION_10_ARN, "true")
    drifted = fake.get(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:9", "include": ["TAGS"]},
    )
    REVISION_9_DRIFTS["another_value_too"](drifted)
    plan, _rendered = _bridge_plan("false", fake=fake)
    assert _step(plan, "task_definition_revision")["blocks"] == [
        f"service_not_on_rollback_target:{NEXT_ARN}"
    ]


@pytest.mark.parametrize("rollback", [8, 9])
def test_a_newest_revision_equal_to_the_serving_one_is_never_registered_green(
    rollback: int,
) -> None:
    """Serving 9 'false', newest 10 'false', parameter 'true': the 'true'
    rendering is not registered, so nothing is 'registered, not deployed'."""

    fake = _stuck_world(NEXT_ARN, "false", REVISION_10_ARN, "false")
    plan, _rendered = _bridge_plan("true", rollback=rollback, fake=fake)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert f"registered_not_deployed:{REVISION_10_ARN}" not in step["compliant"]
    assert "registered_revision_changes_worker_flag" not in step["info"]
    assert step["blocks"] == [
        f"newest_revision_differs_only_in_worker_flag:{REVISION_10_ARN}"
    ]


def test_a_readback_file_without_the_serving_revision_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Service on 10, newest 11, rollback target 9: the serving revision 10 is
    read too, and a readback file without that read fails with its name."""

    fake = _stuck_world(REVISION_10_ARN, "true", REVISION_11_ARN, "false")
    del fake.responses[
        wiring._read_key(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-app:10", "include": ["TAGS"]},
        )
    ]
    transport = wiring.snapshot_transport(fake.snapshot(tmp_path / "readback.json"))
    with pytest.raises(
        wiring.WiringError, match="^readback_missing:ecs.DescribeTaskDefinition$"
    ):
        wiring.collect_readback(
            wiring.ReadOnlyAws(transport),
            _validated(**{WORKER_FLAG: "true"}, app_image_override=OVERRIDE_IMAGE),
            pins=_worker_pins(9),
        )
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(_worker_manifest(9)), encoding="utf-8")
    code, out, err = _run(
        [
            "plan",
            "--params",
            str(_write_params(tmp_path, **{WORKER_FLAG: "true"})),
            "--readback-file",
            str(tmp_path / "readback.json"),
            "--manifest",
            str(manifest),
        ],
        capsys,
    )
    assert code == 2 and out == ""
    assert err.strip() == "blocked: readback_missing:ecs.DescribeTaskDefinition"


def _drift(row_name: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(response: dict[str, Any]) -> None:
        app = evidence._container_map(response["taskDefinition"])["app"]
        for row in app["environment"]:
            if row["name"] == row_name:
                row["value"] = value

    return mutate


REVISION_9_DRIFTS: dict[str, Callable[[dict[str, Any]], None]] = {
    "another_value_too": _drift("ASAAS_WEBHOOK_URL", "https://other.example/x"),
    "flag_unreviewed": _drift(WORKER_FLAG, "yes"),
    "flag_upper_case": _drift(WORKER_FLAG, "FALSE"),
    "flag_missing": lambda r: evidence._container_map(r["taskDefinition"])["app"][
        "environment"
    ].pop(),
    "flag_duplicated": lambda r: evidence._container_map(r["taskDefinition"])["app"][
        "environment"
    ].append({"name": WORKER_FLAG, "value": "true"}),
    "image": lambda r: evidence._container_map(r["taskDefinition"])["app"].__setitem__(
        "image", LIVE_IMAGE
    ),
    "tags": lambda r: r.__setitem__(
        "tags", [{"key": "ManagedBy", "value": "terraform"}]
    ),
    "secret": lambda r: evidence._container_map(r["taskDefinition"])["app"][
        "secrets"
    ].append({"name": "EXTRA", "valueFrom": SECRET_PREFIX + "extra-AbC123"}),
    "otel": lambda r: evidence._container_map(r["taskDefinition"])["otel"].__setitem__(
        "image", "public.ecr.aws/aws-observability/aws-otel-collector:x"
    ),
    "cpu": lambda r: r["taskDefinition"].__setitem__("cpu", "2048"),
}


@pytest.mark.parametrize("drift", sorted(REVISION_9_DRIFTS))
def test_only_a_worker_flag_difference_becomes_a_new_revision(drift: str) -> None:
    fake = FakeAws(applied=True)
    revision_9 = _registered_as(
        NEXT_ARN, _worker_rendering("false", app_image_override=OVERRIDE_IMAGE)
    )
    REVISION_9_DRIFTS[drift](revision_9)
    _put_app_revision(fake, NEXT_ARN, revision_9, latest=True)
    _serve(fake, NEXT_ARN)
    plan, _rendered = _bridge_plan("true", fake=fake)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert step["changes"] == []
    assert f"service_not_on_rollback_target:{NEXT_ARN}" in step["blocks"]
    # Serving that drifted revision 9 while the true rendering is registered
    # as revision 10 is not "registered, not deployed" either.
    rendered = _worker_rendering("true", app_image_override=OVERRIDE_IMAGE)
    _put_app_revision(
        fake, REVISION_10_ARN, _registered_as(REVISION_10_ARN, rendered), latest=True
    )
    plan, _rendered = _bridge_plan("true", fake=fake)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert not any(item.startswith("registered_") for item in step["compliant"])


def test_worker_flag_change_is_exactly_one_reviewed_value_flip() -> None:
    off = _worker_rendering("false", app_image_override=OVERRIDE_IMAGE)
    on = _worker_rendering("true", app_image_override=OVERRIDE_IMAGE)
    assert wiring.worker_flag_change(_registered_as(NEXT_ARN, off), on) == (
        "false",
        "true",
    )
    assert wiring.worker_flag_change(_registered_as(NEXT_ARN, on), off) == (
        "true",
        "false",
    )
    assert wiring.worker_flag_change(_registered_as(NEXT_ARN, off), off) is None
    bogus = copy.deepcopy(on)
    evidence._container_map(bogus)["app"]["environment"][-1]["value"] = "TRUE"
    assert wiring.worker_flag_change(_registered_as(NEXT_ARN, off), bogus) is None
    assert wiring.worker_flag_change(_registered_as(NEXT_ARN, bogus), off) is None
    assert wiring.worker_flag_change({"taskDefinition": "x"}, on) is None


@pytest.mark.parametrize("value", ["yes", "TRUE", " true", True])
def test_render_refuses_an_unreviewed_flag_even_when_called_directly(
    value: Any,
) -> None:
    parameters = _validated()
    parameters[WORKER_FLAG] = value
    with pytest.raises(wiring.WiringError, match=f":{WORKER_FLAG}$"):
        wiring.render_task_definition(
            _task_definition(),
            secret_arns={
                "ASAAS_API_KEY": API_KEY_ARN,
                "ASAAS_WEBHOOK_TOKEN": WEBHOOK_TOKEN_ARN,
                "BILLING_OBSERVABILITY_SMOKE_TOKEN": SMOKE_ARN,
            },
            parameters=parameters,
            pins=_worker_pins(8),
        )


def test_check_passes_with_false_and_true_and_only_the_digest_moves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    results = {}
    for flag in ("false", "true"):
        code, out, err = _worker_cli("check", "bridge", 8, flag, tmp_path, capsys)
        assert code == 0, err
        results[flag] = json.loads(out)
        assert results[flag]["status"] == "pass"
        assert results[flag]["structural_status"] == "pass"
        assert results[flag]["group5_deploy_path_status"] == "pass"
    assert {
        key for key in results["false"] if results["false"][key] != results["true"][key]
    } == {"rendered_request_sha256"}
    assert all(row["result"] == "pass" for row in results["true"]["validators"])


def test_the_protected_path_gates_read_the_same_for_false_and_true() -> None:
    plans = {flag: _bridge_plan(flag)[0] for flag in ("false", "true")}
    gates = {
        flag: _step(plan, "signed_image_build_and_attestation")
        for flag, plan in plans.items()
    }
    assert plans["false"]["gate_blocks"] == plans["true"]["gate_blocks"]
    # Path B (8 -> image + wiring in one protected deploy) was never chosen and
    # is reported with either value; residual 5 stays fixed with either value.
    assert gates["true"]["blocks"] == ["F1_path_b_runtime_fingerprint_changes"]
    for gate in gates.values():
        assert gate["info"]["protected_replacement_keeps_runtime_fingerprint"] is True
        assert gate["info"]["d3_environment_values_fingerprinted"] is True
    # The flag value is part of the runtime fingerprint: the protected deploy
    # binds the value of the revision it starts from.
    assert (
        gates["false"]["info"]["runtime_fingerprint_candidate"]
        != gates["true"]["info"]["runtime_fingerprint_candidate"]
    )
    assert (
        gates["false"]["info"]["runtime_fingerprint_rollback_target"]
        == gates["true"]["info"]["runtime_fingerprint_rollback_target"]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["task_definition"]["app_environment_added"].__setitem__(
            WORKER_FLAG, {"value": "false"}
        ),
        lambda m: m["task_definition"]["app_environment_added"].__setitem__(
            WORKER_FLAG, {"value": "true"}
        ),
        lambda m: m["task_definition"]["app_environment_added"].__setitem__(
            WORKER_FLAG, {"parameter": "ASAAS_WEBHOOK_URL"}
        ),
        lambda m: m["parameters"]["required"].pop(WORKER_FLAG),
        lambda m: m["parameters"]["required"][WORKER_FLAG].__setitem__(
            "validator", "any_string"
        ),
        lambda m: m["parameters"]["optional"].__setitem__(
            WORKER_FLAG, m["parameters"]["required"].pop(WORKER_FLAG)
        ),
    ],
)
def test_manifest_validator_pins_the_worker_flag_to_its_parameter(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    manifest = wiring.load_yaml(MANIFEST)
    mutate(manifest)
    with pytest.raises(wiring.WiringError, match="^manifest_"):
        wiring.validate_manifest(manifest)


def test_check_manifest_accepts_the_reviewed_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = _run(["check-manifest", "--params", str(PARAMS)], capsys)
    assert code == 0, err
    assert json.loads(out)["status"] == "valid"


# ---------------------------------------------------------------------------
# The image bump: a new app image digest from a serving registered D3 revision
# ---------------------------------------------------------------------------

REGISTRY = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"
STAGING_REPOSITORY = f"{REGISTRY}/neurostack-staging"
BRIDGE_DIGEST = (
    "sha256:18358ce728c465ffd0d9cf6d0e9cf4641c32f6bab9dfa2905729c5495bcf7f24"
)
# The next image, pushed by image ID to the same repository (owner readback).
NEW_DIGEST = "sha256:f6603a5ee0f8293abfe290d639cfc241a8ac864fe16d0e31393d20d7f0b4b159"
BRIDGE_IMAGE = f"{STAGING_REPOSITORY}@{BRIDGE_DIGEST}"
NEW_IMAGE = f"{STAGING_REPOSITORY}@{NEW_DIGEST}"
OTHER_DIGEST = "sha256:" + "3" * 64
OTHER_IMAGE = f"{STAGING_REPOSITORY}@{OTHER_DIGEST}"


def _with_images(fake: FakeAws) -> FakeAws:
    """ECR knows the bridge, the new and one other digest of neurostack-staging."""

    for digest in (BRIDGE_DIGEST, NEW_DIGEST, OTHER_DIGEST):
        fake.put(
            "ecr",
            "DescribeImages",
            {
                "repositoryName": "neurostack-staging",
                "imageIds": [{"imageDigest": digest}],
            },
            {"imageDetails": [{"imageDigest": digest}]},
        )
    return fake


def _image_rendering(image: str, flag: str = "true") -> dict[str, Any]:
    """The rendering of the D3 world with ``image`` and the flag ``flag``."""

    _plan_result, rendered = _plan(
        _with_images(FakeAws(applied=True)),
        pins=_worker_pins(8),
        app_image_override=image,
        **{WORKER_FLAG: flag},
    )
    assert rendered is not None
    return rendered


def _image_world(serving: str, *revisions: tuple[str, str, str]) -> FakeAws:
    """The service on ``serving``; each (ARN, image, flag) registered from its
    rendering, the last one the newest registered revision."""

    fake = _with_images(FakeAws(applied=True))
    for index, (arn, image, flag) in enumerate(revisions):
        _put_app_revision(
            fake,
            arn,
            _registered_as(arn, _image_rendering(image, flag)),
            latest=index == len(revisions) - 1,
        )
    _serve(fake, serving)
    return fake


# Live since the flag deploy: revision 9 (bridge, 'false'), revision 10
# (bridge, 'true') serving and the newest.
FLAG_REVISIONS = (
    (NEXT_ARN, BRIDGE_IMAGE, "false"),
    (REVISION_10_ARN, BRIDGE_IMAGE, "true"),
)
REVISION_12_ARN = SOURCE_ARN.rsplit(":", 1)[0] + ":12"


def _live_flag_world() -> FakeAws:
    return _image_world(REVISION_10_ARN, *FLAG_REVISIONS)


def _image_plan(
    fake: FakeAws, *, image: str | None = NEW_IMAGE, flag: str = "true", rollback: int
) -> tuple[dict[str, Any], Any]:
    return _plan(
        fake,
        pins=_worker_pins(rollback),
        app_image_override=image,
        **{WORKER_FLAG: flag},
    )


def _image_change(image_from: str, image_to: str) -> dict[str, str]:
    return {
        "repository": STAGING_REPOSITORY,
        "from": image_from.split("@", 1)[1],
        "to": image_to.split("@", 1)[1],
    }


@pytest.mark.parametrize("rollback", [8, 9, 10])
def test_plan_on_revision_10_with_a_new_digest_proposes_an_image_bump(
    rollback: int,
) -> None:
    fake = _live_flag_world()
    plan, rendered = _image_plan(fake, rollback=rollback)
    assert rendered is not None
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "ready", plan["blocks"]
    assert step["state"] == "would_change" and step["blocks"] == []
    assert plan["would_change"] == ["task_definition_revision:register_task_definition"]
    assert step["changes"] == [
        {
            "action": "register_task_definition",
            "family": "neurostack-staging-app",
            "derived_from": SOURCE_ARN,
            "replaces": REVISION_10_ARN,
            "app_image": _image_change(BRIDGE_IMAGE, NEW_IMAGE),
            "request_sha256": wiring._sha256(rendered),
            "service_changes": "none (service_deployment is a separate gate)",
        }
    ]
    assert wiring.renderable(plan, rendered) is rendered
    # The rendering is revision 10 with the app image changed, nothing else.
    revision_10 = wiring._request_from_response(
        fake.get(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-app:10", "include": ["TAGS"]},
        )
    )
    assert evidence._container_map(revision_10)["app"]["image"] == BRIDGE_IMAGE
    evidence._container_map(revision_10)["app"]["image"] = NEW_IMAGE
    assert revision_10 == rendered
    assert evidence._env_map(evidence._container_map(rendered)["app"])[WORKER_FLAG] == (
        "true"
    )
    # Deploying it before the re-pin to 10 would leave revision 9 (the worker
    # off) or 8 as the way back: a gate block unless the manifest pins 10.
    gate = "rollback_target_not_repinned_before_image_bump"
    assert (gate in _step(plan, "service_deployment")["blocks"]) == (rollback != 10)


@pytest.mark.parametrize("rollback", [8, 9, 10])
def test_a_registered_image_bump_is_reported_until_and_after_it_serves(
    rollback: int,
) -> None:
    fake = _image_world(
        REVISION_10_ARN, *FLAG_REVISIONS, (REVISION_11_ARN, NEW_IMAGE, "true")
    )
    fake.calls.clear()
    plan, _rendered = _image_plan(fake, rollback=rollback)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "compliant", plan["blocks"]
    assert step["changes"] == [] and step["blocks"] == []
    assert f"registered_not_deployed:{REVISION_11_ARN}" in step["compliant"]
    assert step["info"]["registered_revision_changes_app_image"] == {
        "serving": REVISION_10_ARN,
        **_image_change(BRIDGE_IMAGE, NEW_IMAGE),
    }
    assert "registered_revision_changes_worker_flag" not in step["info"]
    # The serving revision 10 is read exactly once (as the rollback target, or
    # as the serving revision when the rollback target is 8 or 9).
    ten = {"taskDefinition": "neurostack-staging-app:10", "include": ["TAGS"]}
    assert [call[2] for call in fake.calls].count(ten) == 1
    gate = "rollback_target_not_repinned_before_image_bump"
    assert (gate in _step(plan, "service_deployment")["blocks"]) == (rollback != 10)

    _serve(fake, REVISION_11_ARN)
    plan, _rendered = _image_plan(fake, rollback=rollback)
    step = _step(plan, "task_definition_revision")
    assert f"registered_and_deployed:{REVISION_11_ARN}" in step["compliant"]
    deployment = _step(plan, "service_deployment")
    if rollback == 9:
        # Deployed while the manifest still pins 9 with its note: the way
        # back would turn the worker off, so the deployment gate still blocks.
        assert gate in deployment["blocks"]
    elif rollback == 10:
        assert plan["status"] == "compliant"
        assert gate not in deployment["blocks"]
    if rollback == 10:
        # Once 11 serves, the way back is revision 10.
        assert plan["target"]["rollback_target"] == REVISION_10_ARN
        assert deployment["info"]["rollback_target"] == REVISION_10_ARN
        assert "rollback_target_active_and_digest_pinned" in deployment["compliant"]

    # Rolled back to revision 10 with the override unchanged: 11 is again
    # registered, not deployed.
    _serve(fake, REVISION_10_ARN)
    plan, _rendered = _image_plan(fake, rollback=rollback)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "compliant"
    assert f"registered_not_deployed:{REVISION_11_ARN}" in step["compliant"]


def test_the_worker_flag_branches_still_work_from_revision_11() -> None:
    """Rollback target 10, service on the image bump revision 11."""

    fake = _image_world(
        REVISION_11_ARN, *FLAG_REVISIONS, (REVISION_11_ARN, NEW_IMAGE, "true")
    )
    plan, rendered = _image_plan(fake, flag="false", rollback=10)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "ready", plan["blocks"]
    [change] = step["changes"]
    assert change["replaces"] == REVISION_11_ARN
    assert change["worker_flag"] == {"name": WORKER_FLAG, "from": "true", "to": "false"}
    assert "app_image" not in change
    assert evidence._container_map(rendered)["app"]["image"] == NEW_IMAGE

    # Registered as 12 while 11 serves: registered, not deployed (11 is read).
    _put_app_revision(
        fake, REVISION_12_ARN, _registered_as(REVISION_12_ARN, rendered), latest=True
    )
    fake.calls.clear()
    plan, _rendered = _image_plan(fake, flag="false", rollback=10)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "compliant", plan["blocks"]
    assert f"registered_not_deployed:{REVISION_12_ARN}" in step["compliant"]
    assert step["info"]["registered_revision_changes_worker_flag"] == {
        "serving": REVISION_11_ARN,
        "from": "true",
        "to": "false",
    }
    eleven = {"taskDefinition": "neurostack-staging-app:11", "include": ["TAGS"]}
    assert [call[2] for call in fake.calls].count(eleven) == 1

    # The parameter back to 'true' with 12 abandoned: the flag's stuck state.
    plan, _rendered = _image_plan(fake, flag="true", rollback=10)
    assert _step(plan, "task_definition_revision")["blocks"] == [
        f"newest_revision_differs_only_in_worker_flag:{REVISION_12_ARN}"
    ]


# (serving, newest registered (image, flag), rendered image): the image bump
# abandoned or rolled back with the override set back to the bridge digest,
# and an unwanted image registered while another one is rendered.
IMAGE_STUCK_STATES = [
    (REVISION_10_ARN, (NEW_IMAGE, "true"), BRIDGE_IMAGE),
    (REVISION_10_ARN, (OTHER_IMAGE, "true"), NEW_IMAGE),
]


@pytest.mark.parametrize("rollback", [9, 10])
@pytest.mark.parametrize("serving,newest,image", IMAGE_STUCK_STATES)
def test_a_newest_revision_differing_only_in_the_image_blocks_under_its_own_name(
    serving: str, newest: tuple[str, str], image: str, rollback: int
) -> None:
    fake = _image_world(serving, *FLAG_REVISIONS, (REVISION_11_ARN, *newest))
    plan, _rendered = _image_plan(fake, image=image, rollback=rollback)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert step["blocks"] == [
        f"newest_revision_differs_only_in_app_image:{REVISION_11_ARN}"
    ]
    assert step["changes"] == []
    assert not any(item.startswith("registered_") for item in step["compliant"])
    assert step["info"]["newest_revision_app_image"] == {
        "newest": REVISION_11_ARN,
        "repository": STAGING_REPOSITORY,
        "newest_digest": newest[0].split("@", 1)[1],
        "rendered_digest": image.split("@", 1)[1],
        "serving": serving,
    }
    assert "deregister" not in json.dumps(plan).lower()


def test_render_taskdef_refuses_the_image_stuck_state_with_its_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _image_world(
        REVISION_10_ARN, *FLAG_REVISIONS, (REVISION_11_ARN, NEW_IMAGE, "true")
    )
    raw = _parameters(app_image_override=BRIDGE_IMAGE)
    params = tmp_path / "params.yaml"
    params.write_text(yaml.safe_dump(raw), encoding="utf-8")
    args = [
        "--params",
        str(params),
        "--readback-file",
        str(fake.snapshot(tmp_path / "readback.json")),
        "--manifest",
        str(MANIFEST),
    ]
    for command in ("render-taskdef", "check"):
        code, out, err = _run([command, *args], capsys)
        assert code == 2 and out == ""
        assert (
            "blocked: plan_blocked:"
            f"newest_revision_differs_only_in_app_image:{REVISION_11_ARN}" in err
        )


def _app_of(response: dict[str, Any]) -> dict[str, Any]:
    return evidence._container_map(response["taskDefinition"])["app"]


# Anything on revision 10 besides its app image: never an image bump.
IMAGE_BUMP_REFUSALS: dict[str, Callable[[dict[str, Any]], None]] = {
    **REVISION_9_DRIFTS,
    "image_in_another_repository": lambda r: _app_of(r).__setitem__(
        "image", BRIDGE_IMAGE.replace("/neurostack-staging@", "/neurostack-other@")
    ),
    "image_by_tag_and_digest": lambda r: _app_of(r).__setitem__(
        "image", BRIDGE_IMAGE.replace("@", ":bridge@")
    ),
    "image_digest_malformed": lambda r: _app_of(r).__setitem__(
        "image", f"{STAGING_REPOSITORY}@sha256:{BRIDGE_DIGEST[7:].upper()}"
    ),
    "image_and_worker_flag_together": _drift(WORKER_FLAG, "false"),
    "secret_reference": lambda r: _app_of(r)["secrets"][-1].__setitem__(
        "valueFrom", SECRET_PREFIX + "billing-observability-smoke-token-ZzZ999"
    ),
    "task_role": lambda r: r["taskDefinition"].__setitem__(
        "taskRoleArn", evidence.TASK_ROLE_ARN + "-other"
    ),
    "app_command": lambda r: _app_of(r).__setitem__("command", ["sh"]),
}


@pytest.mark.parametrize("refusal", sorted(IMAGE_BUMP_REFUSALS))
def test_only_an_image_digest_difference_becomes_an_image_bump(refusal: str) -> None:
    fake = _live_flag_world()
    revision_10 = fake.get(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:10", "include": ["TAGS"]},
    )
    IMAGE_BUMP_REFUSALS[refusal](revision_10)
    _put_app_revision(fake, REVISION_10_ARN, revision_10, latest=True)
    plan, _rendered = _image_plan(fake, rollback=10)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert step["changes"] == []
    assert step["blocks"] == [f"service_not_on_rollback_target:{REVISION_10_ARN}"]
    assert "rollback_target_not_repinned_before_image_bump" not in (
        _step(plan, "service_deployment")["blocks"]
    )
    # Serving that drifted revision 10 while the new rendering is registered
    # as revision 11 is not "registered, not deployed" either.
    _put_app_revision(
        fake,
        REVISION_11_ARN,
        _registered_as(REVISION_11_ARN, _image_rendering(NEW_IMAGE)),
        latest=True,
    )
    plan, _rendered = _image_plan(fake, rollback=10)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert not any(item.startswith("registered_") for item in step["compliant"])


def test_a_rendering_by_tag_is_never_an_image_bump() -> None:
    """Without app_image_override the rendering keeps revision 8's tag."""

    plan, _rendered = _image_plan(_live_flag_world(), image=None, rollback=10)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert step["changes"] == []
    assert step["blocks"] == [f"service_not_on_rollback_target:{REVISION_10_ARN}"]


def test_app_image_change_is_exactly_one_digest_move() -> None:
    bridge = _image_rendering(BRIDGE_IMAGE)
    new = _image_rendering(NEW_IMAGE)
    assert wiring.app_image_change(_registered_as(REVISION_10_ARN, bridge), new) == (
        BRIDGE_IMAGE,
        NEW_IMAGE,
    )
    assert wiring.app_image_change(_registered_as(REVISION_11_ARN, new), bridge) == (
        NEW_IMAGE,
        BRIDGE_IMAGE,
    )
    assert wiring.app_image_change(_registered_as(REVISION_10_ARN, new), new) is None
    flag_off = _image_rendering(NEW_IMAGE, "false")
    assert (
        wiring.app_image_change(_registered_as(REVISION_10_ARN, bridge), flag_off)
        is None
    )
    tagged = copy.deepcopy(new)
    evidence._container_map(tagged)["app"]["image"] = LIVE_IMAGE
    assert (
        wiring.app_image_change(_registered_as(REVISION_10_ARN, bridge), tagged) is None
    )
    assert wiring.app_image_change(_registered_as(REVISION_10_ARN, tagged), new) is None
    other = copy.deepcopy(new)
    evidence._container_map(other)["app"]["image"] = NEW_IMAGE.replace(
        "/neurostack-staging@", "/neurostack-other@"
    )
    assert (
        wiring.app_image_change(_registered_as(REVISION_10_ARN, bridge), other) is None
    )
    assert wiring.app_image_change({"taskDefinition": "x"}, new) is None


@pytest.mark.parametrize(
    "rendered_image",
    [
        NEW_IMAGE.replace("@", ":next@"),
        f"{STAGING_REPOSITORY}@sha256:{NEW_DIGEST[7:].upper()}",
    ],
)
def test_app_image_change_requires_a_rendered_digest_without_tag(
    rendered_image: str,
) -> None:
    """The rendered side is validated like the registered one."""

    bridge = _image_rendering(BRIDGE_IMAGE)
    rendered = copy.deepcopy(bridge)
    evidence._container_map(rendered)["app"]["image"] = rendered_image
    assert (
        wiring.app_image_change(_registered_as(REVISION_10_ARN, bridge), rendered)
        is None
    )


def test_a_drifted_serving_revision_is_never_the_image_stuck_state() -> None:
    """The newest revision differs from the rendering in the image alone, but
    the serving revision 10 differs from it in more than the image: not the
    stuck state, the service is off its known revisions."""

    fake = _image_world(
        REVISION_10_ARN, *FLAG_REVISIONS, (REVISION_11_ARN, NEW_IMAGE, "true")
    )
    revision_10 = fake.get(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": "neurostack-staging-app:10", "include": ["TAGS"]},
    )
    _app_of(revision_10)["command"] = ["sh"]
    _put_app_revision(fake, REVISION_10_ARN, revision_10, latest=False)
    plan, _rendered = _image_plan(fake, image=BRIDGE_IMAGE, rollback=10)
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "blocked"
    assert step["blocks"] == [f"service_not_on_rollback_target:{REVISION_10_ARN}"]
    assert "newest_revision_app_image" not in step["info"]


def test_only_rollback_target_10_without_a_note_clears_the_image_bump_gate() -> None:
    """The validator never accepts revision 10 with a re-pin note; the gate
    still requires the good state itself, not the validator."""

    pins = dataclasses.replace(
        _worker_pins(10), rollback_repin_before=wiring.ROLLBACK_REPIN_BEFORE_IMAGE_BUMP
    )
    plan, _rendered = _plan(
        _live_flag_world(),
        pins=pins,
        app_image_override=NEW_IMAGE,
        **{WORKER_FLAG: "true"},
    )
    assert "rollback_target_not_repinned_before_image_bump" in (
        _step(plan, "service_deployment")["blocks"]
    )


def test_the_live_state_before_the_re_pin_never_reports_the_image_bump_gate() -> None:
    """Revision 10 serves the bridge image, the manifest is still at 9 with its
    note and the override is the bridge digest: no image bump, so no gate
    (revision 10 and the rollback target 9 run the same image)."""

    plan, _rendered = _image_plan(_live_flag_world(), image=BRIDGE_IMAGE, rollback=9)
    assert plan["status"] == "compliant", plan["blocks"]
    assert (
        f"registered_and_deployed:{REVISION_10_ARN}"
        in _step(plan, "task_definition_revision")["compliant"]
    )
    gate = "rollback_target_not_repinned_before_image_bump"
    assert gate not in _step(plan, "service_deployment")["blocks"]
    assert gate not in [row["reason"] for row in plan["gate_blocks"]]


@pytest.mark.parametrize("rollback", [9, 10])
def test_a_serving_image_from_another_repository_is_gated_like_a_bump(
    rollback: int,
) -> None:
    """Revision 11, registered and deployed outside the planner with an app
    image in another repository: with rollback target 9 the gate blocks, with
    10 it does not."""

    digest = "sha256:" + "4" * 64
    image = f"{REGISTRY}/neurostack-other@{digest}"
    fake = _image_world(REVISION_10_ARN, *FLAG_REVISIONS)
    fake.put(
        "ecr",
        "DescribeImages",
        {"repositoryName": "neurostack-other", "imageIds": [{"imageDigest": digest}]},
        {"imageDetails": [{"imageDigest": digest}]},
    )
    _plan_result, rendered = _image_plan(fake, image=image, rollback=rollback)
    assert rendered is not None
    _put_app_revision(
        fake, REVISION_11_ARN, _registered_as(REVISION_11_ARN, rendered), latest=True
    )
    _serve(fake, REVISION_11_ARN)
    plan, _rendered = _image_plan(fake, image=image, rollback=rollback)
    assert (
        f"registered_and_deployed:{REVISION_11_ARN}"
        in _step(plan, "task_definition_revision")["compliant"]
    )
    gate = "rollback_target_not_repinned_before_image_bump"
    assert (gate in _step(plan, "service_deployment")["blocks"]) == (rollback == 9)


def test_a_flag_change_never_reports_the_image_bump_gate() -> None:
    """The pending 9 -> 10 re-pin only gates an image bump's deploy."""

    plan, _rendered = _bridge_plan("true", rollback=9)
    assert plan["status"] == "ready"
    assert "rollback_target_not_repinned_before_image_bump" not in (
        _step(plan, "service_deployment")["blocks"]
    )


def test_the_rollback_target_may_be_re_pinned_from_9_to_10_only() -> None:
    for revision in (8, 9, 10):
        wiring.validate_manifest(_worker_manifest(revision))
    flag = wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG
    note_to_10 = copy.deepcopy(WORKER_ROLLBACK_TARGETS[9]["repin"])
    invalid: list[dict[str, Any]] = [
        # 10 is the last pin: no re-pin from it, no revision after it.
        {**WORKER_ROLLBACK_TARGETS[10], "repin": {**note_to_10, "revision": 11}},
        {"revision": 11, "effective_after": flag},
        {"revision": 10, "effective_after": wiring.ROLLBACK_EFFECTIVE_BRIDGE},
        # No skipping: 8 re-pins to 9 only, 9 to 10 only.
        {**WORKER_ROLLBACK_TARGETS[8], "repin": note_to_10},
        {"revision": 8, "effective_after": flag},
        {**WORKER_ROLLBACK_TARGETS[9], "repin": {**note_to_10, "revision": 11}},
        # The 9 -> 10 note names its own step and the flag it protects.
        {
            **WORKER_ROLLBACK_TARGETS[9],
            "repin": {**note_to_10, "before": "migration_036"},
        },
        {
            **WORKER_ROLLBACK_TARGETS[9],
            "repin": {**note_to_10, "why": "Revision 8 reads columns 036 drops."},
        },
        {
            **WORKER_ROLLBACK_TARGETS[9],
            "repin": {
                **note_to_10,
                "effective_after": wiring.ROLLBACK_EFFECTIVE_BRIDGE,
            },
        },
        {"revision": 9, "effective_after": wiring.ROLLBACK_EFFECTIVE_BRIDGE},
    ]
    for target in invalid:
        manifest = wiring.load_yaml(MANIFEST)
        manifest["rollback_target"] = copy.deepcopy(target)
        with pytest.raises(
            wiring.WiringError, match="manifest_rollback_target_invalid"
        ):
            wiring.validate_manifest(manifest)


def _repository_params_with_image(directory: Path, image: str) -> Path:
    """The operator's local copy of the repository file with only
    app_image_override set (the flag line is the repository's own)."""

    text = PARAMS.read_text(encoding="utf-8")
    line = re.compile(r"^  app_image_override: null$", re.MULTILINE)
    assert len(line.findall(text)) == 1
    path = directory / f"params-{image.rsplit(':', 1)[1][:12]}.yaml"
    path.write_text(line.sub(f"  app_image_override: {image}", text), encoding="utf-8")
    return path


def test_the_repository_parameters_propose_the_image_bump(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live state (revision 10 serving the bridge image with 'true', the
    newest), the reviewed manifest (rollback target 10) and the repository
    file with only the apply-time image set to the new digest: plan proposes
    one new revision whose only difference from revision 10 is the app image,
    render-taskdef prints it and check passes. With the bridge digest instead,
    revision 10 is registered and deployed."""

    fake = _live_flag_world()
    snapshot = fake.snapshot(tmp_path / "readback.json")
    outputs: dict[str, dict[str, str]] = {}
    for image in (NEW_IMAGE, BRIDGE_IMAGE):
        params = _repository_params_with_image(tmp_path, image)
        outputs[image] = {}
        for command in ("plan", "render-taskdef", "check"):
            code, out, err = _run(
                [
                    command,
                    "--params",
                    str(params),
                    "--readback-file",
                    str(snapshot),
                    "--manifest",
                    str(MANIFEST),
                ],
                capsys,
            )
            assert code == 0, (image, command, err)
            outputs[image][command] = out

    plan = json.loads(outputs[NEW_IMAGE]["plan"])
    step = _step(plan, "task_definition_revision")
    assert plan["status"] == "ready"
    assert plan["would_change"] == ["task_definition_revision:register_task_definition"]
    assert plan["parameters"] == {
        **plan["parameters"],
        WORKER_FLAG: "true",
        "app_image_override": NEW_IMAGE,
    }
    assert plan["target"]["rollback_target"] == REVISION_10_ARN
    assert plan["target"]["current_task_definition"] == REVISION_10_ARN
    assert step["state"] == "would_change" and step["blocks"] == []
    [change] = step["changes"]
    assert change["replaces"] == REVISION_10_ARN
    assert change["app_image"] == {
        "repository": STAGING_REPOSITORY,
        "from": BRIDGE_DIGEST,
        "to": NEW_DIGEST,
    }
    assert "worker_flag" not in change
    assert change["service_changes"] == "none (service_deployment is a separate gate)"
    rendered = json.loads(outputs[NEW_IMAGE]["render-taskdef"])
    assert change["request_sha256"] == wiring._sha256(rendered)
    deployment = _step(plan, "service_deployment")
    assert "rollback_target_active_and_digest_pinned" in deployment["compliant"]
    assert "rollback_target_not_repinned_before_image_bump" not in deployment["blocks"]

    # The rendered request differs from revision 10 in the app image alone.
    revision_10 = wiring._request_from_response(
        fake.get(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": "neurostack-staging-app:10", "include": ["TAGS"]},
        )
    )
    before = json.dumps(revision_10, indent=2, sort_keys=True).splitlines()
    after = json.dumps(rendered, indent=2, sort_keys=True).splitlines()
    assert [
        (old.strip(), new.strip())
        for old, new in zip(before, after, strict=True)
        if old != new
    ] == [(f'"image": "{BRIDGE_IMAGE}",', f'"image": "{NEW_IMAGE}",')]
    check = json.loads(outputs[NEW_IMAGE]["check"])
    assert check["status"] == "pass" and check["structural_status"] == "pass"
    assert check["rendered_request_sha256"] == change["request_sha256"]

    control = json.loads(outputs[BRIDGE_IMAGE]["plan"])
    assert control["status"] == "compliant"
    assert (
        f"registered_and_deployed:{REVISION_10_ARN}"
        in _step(control, "task_definition_revision")["compliant"]
    )


# Statements of the rollback target from before the 9 -> 10 re-pin; none may
# come back while the manifest pins 10.
RETIRED_ROLLBACK_STATEMENTS = (
    "revision 9 is the last pin the planner allows",
    "its only allowed move is 8 to 9",
    "This is the final pin of the planner",
    "rollback_target stays revision 9",
    "re-pinned to revision 9, `ACTIVE`",
    "`rollback_target` revision 9 with no pending re-pin",
)


# The observation of revision 10 closes 6 h after its deployment completed
# (2026-10-25 01:51 UTC).
REPIN_9_TO_10_EARLIEST_CLOSE = datetime.datetime(2026, 10, 25, 7, 51)


def _closed_at_is_valid(value: str) -> bool:
    """The manifest marker: the placeholder, or YYYY-MM-DDTHH:MMZ at or after
    the earliest close."""

    if value == "<filled in at merge>":
        return True
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", value) is None:
        return False
    try:
        closed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%MZ")
    except ValueError:
        return False
    return closed >= REPIN_9_TO_10_EARLIEST_CLOSE


@pytest.mark.parametrize(
    "value,valid",
    [
        ("<filled in at merge>", True),
        ("2026-10-25T07:51Z", True),
        ("2026-10-25T23:59Z", True),
        ("2026-10-26T00:10Z", True),
        ("2026-11-02T09:00Z", True),
        ("2026-10-25T07:50Z", False),
        ("2026-10-25T01:51Z", False),
        ("2026-10-24T23:59Z", False),
        ("2026-10-25T7:51Z", False),
        ("2026-10-25T07:51", False),
        ("2026-10-25 07:51Z", False),
        ("2026-10-25T07:51:00Z", False),
        ("2026-10-25T25:00Z", False),
        ("<filled in later>", False),
        ("", False),
    ],
)
def test_the_closed_at_marker_accepts_only_a_close_at_or_after_07_51(
    value: str, valid: bool
) -> None:
    assert _closed_at_is_valid(value) is valid


def test_the_runbook_documents_the_image_bump() -> None:
    runbook = _flat(WORKER_FLAG_RUNBOOK)
    raw = WORKER_FLAG_RUNBOOK.read_text(encoding="utf-8")
    assert "\n### The image bump (`app_image_override`)\n" in raw
    assert (
        raw.index("### The outbox worker flag")
        < raw.index("### The image bump (`app_image_override`)")
        < raw.index("### Ordered procedure")
    )
    assert "(#the-image-bump-app_image_override)" in runbook
    section = _between(
        runbook, "### The image bump (`app_image_override`)", "### Ordered procedure"
    )
    for phrase in (
        "moves the service to a new application image and changes nothing else",
        "It never carries a worker flag change: the flag and an image never share"
        " a revision or a deploy (owner decision 2026-10-22)",
        "Any other difference blocks (`service_not_on_rollback_target:<ARN>`): an"
        " image by tag, an image in another repository, a malformed digest, and a"
        " rendering that changes the image and the flag together.",
        "`app_image_override` stays `null` in the repository",
        "`replaces: <revision 10 ARN>`",
        "`app_image: {repository, from: <bridge digest>, to: <new digest>}`, no"
        " `worker_flag`",
        "`registered_revision_changes_app_image` naming revision 10",
        "`rollback_target` stays revision 10",
        "**Rollback** is the service back to revision 10, the rollback target",
        "No revision is registered for it",
        "`newest_revision_differs_only_in_app_image:<newest ARN>`",
        "Recovery is an owner decision, carried out by the operator and never by"
        " these tools",
        "`rollback_target_not_repinned_before_image_bump`",
    ):
        assert phrase in section, phrase
    _in_order(
        section,
        [
            "1. **Build and smoke.**",
            "2. **Push by image ID.**",
            "never rebuild for the push",
            "3. **Verify the digest and the scan.**",
            "describe-image-scan-findings",
            "4. **Plan, render, check, register.**",
            "Register it as revision 11 (`ManagedBy=manual`, by the operator)",
            "5. **Database read, before the deploy.**",
            "6. **Deploy** revision 11: the `service_deployment` gate (separate"
            " owner approval)",
            "7. **Post-deploy hook.**",
            "8. **Observe.**",
            "Take the database read again after the deploy and compare it with"
            " the read of step 5",
        ],
    )
    for phrase in (
        # The template regeneration belongs to the protected path only.
        "Step 6 of the flag procedure (regenerate the protected template from"
        " revision 10 before the E7 image) applies only to a deploy through the"
        " protected path. The manual image bump below is outside that path",
        "The template was regenerated from revision 10 on 2026-10-25; once"
        " revision 11 serves, it must be regenerated again, from revision 11,"
        " before any later protected deploy.",
        "or, when its image is the one wanted, set the override to its digest"
        " and deploy it (step 6)",
        "unless the manifest pins revision 10 with no pending re-pin, `plan` also"
        " reports the gate block `rollback_target_not_repinned_before_image_bump`"
        " on `service_deployment`",
        "Deploy only when `plan` does not report"
        " `rollback_target_not_repinned_before_image_bump`.",
        "with revision 8 pinned there is no digest to compare with and no gate, a"
        " state the documented order never produces",
        "That reason names the serving revision (revision 10 here) and does not"
        " say which fields differ",
        "The flag code behaves the same.",
        "Setting the override back to the bridge digest while revision 11 still"
        " serves is not a rollback: `plan` then proposes a new revision (expected"
        " `:12`) that replaces revision 11 and is identical to revision 10",
    ):
        assert phrase in section, phrase

    repin = _between(
        runbook,
        "**Re-pinning the rollback target (9 to 10).**",
        "What the current image does with the D3 names",
    )
    for phrase in (
        f"`effective_after: {wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG}`",
        f"(`before: {wiring.ROLLBACK_REPIN_BEFORE_IMAGE_BUMP}`",
        "**before** the [image bump](#the-image-bump-app_image_override) is"
        " deployed",
        "allows no pin after 10, and 8 to 10 is not a move",
        "**Merge condition of the re-pin (9 to 10).** The manifest edit above"
        " merges only after revision 10's observation has closed: **6 hours after"
        " its deployment completed (2026-10-25 01:51 UTC), so at 2026-10-25"
        " 07:51 UTC**, and before the image bump is deployed.",
        "8 `billing fact worker started` lines and no `billing fact worker tick"
        " failed`, no `billing fact acknowledgement overdue` and no ERROR line,"
        " and the service stayed 2/2 healthy with no task restart, no 5xx and no"
        " unhealthy target.",
        "The orchestrator makes the final check at 07:51 UTC and records it",
        "and for a serving one when revision 9 is pinned",
        "With revision 8 pinned a serving image bump is not detected",
        '--filter-pattern "$pattern"',
        "HTTPCode_Target_5XX_Count UnHealthyHostCount",
    ):
        assert phrase in repin, phrase
    # The gate is stated in the steps, never in the stuck-state paragraph.
    stuck = section[
        section.index(
            "**Newest revision differing only in the image (blocks, never green).**"
        ) :
    ]
    assert "rollback_target_not_repinned_before_image_bump" not in stuck

    # Comments read as prose: the markers that start each comment line go.
    manifest_text = " ".join(
        re.sub(r"^\s*#", "", line)
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
    )
    manifest_text = " ".join(manifest_text.split())
    for path, text in (
        (WORKER_FLAG_RUNBOOK, runbook),
        (MANIFEST, manifest_text),
        (
            ROOT / "docs/runbooks/asaas-rollback.md",
            _flat(ROOT / "docs/runbooks/asaas-rollback.md"),
        ),
    ):
        for retired in RETIRED_ROLLBACK_STATEMENTS:
            assert retired not in text, (path.name, retired)
    assert "Revision 10 is the final pin of the planner" in manifest_text
    # The 9 -> 10 re-pin merges only after the 6 h observation closes; the
    # orchestrator fills the actual closing time into this one marker before
    # the merge (it is never invented here).
    assert "so at 2026-10-25 07:51 UTC" in manifest_text
    marker = re.compile(r"^# closed_at: (?P<value>.+)$", re.MULTILINE)
    [value] = marker.findall(MANIFEST.read_text(encoding="utf-8"))
    assert _closed_at_is_valid(value), value
    for text in (manifest_text, runbook):
        assert "closed_at: YYYY-MM-DDTHH:MMZ" in text
    rollback_doc = _flat(ROOT / "docs/runbooks/asaas-rollback.md")
    for text in (runbook, rollback_doc):
        assert "2026-10-25 07:51 UTC" in text
        assert "manifest marker `closed_at` before the merge" in text
    # The new digest is an apply-time input: no procedure, parameter, manifest
    # or tool names it. Since revision 11 serves (2026-10-25), the runbook's
    # state section records it as operator evidence, and only there.
    for path in (
        WORKER_FLAG_CONFIGURATION,
        ROOT / "docs/runbooks/asaas-rollback.md",
        PARAMS,
        MANIFEST,
        SCRIPT,
        ROOT / "scripts/asaas_migration_window.py",
    ):
        assert NEW_DIGEST[7:] not in path.read_text(encoding="utf-8"), path.name
    raw_runbook = WORKER_FLAG_RUNBOOK.read_text(encoding="utf-8")
    state = _runbook_state_section()
    assert NEW_DIGEST in state
    assert NEW_DIGEST[7:] not in raw_runbook.replace(state, "")
    assert "\n  app_image_override: null\n" in PARAMS.read_text(encoding="utf-8")
