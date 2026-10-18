"""Group 9 D3/D4 production wiring: read-only planner, manifest and template.

Every test runs against injected fake AWS responses; nothing here reaches AWS.
"""

from __future__ import annotations

import copy
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

from scripts import asaas_producer_rollout_evidence as evidence
from scripts import asaas_production_wiring as wiring
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
SOURCE_ARN = wiring.ROLLBACK_TASK_DEFINITION_ARN
NEXT_ARN = SOURCE_ARN.replace(":8", ":9")
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


def _plan(
    fake: FakeAws, *, applied: dict[str, Any] | None = None, **changes: Any
) -> tuple[dict[str, Any], Any]:
    parameters = _validated(**changes)
    aws = wiring.ReadOnlyAws(fake.transport)
    readback = wiring.collect_readback(aws, parameters)
    return wiring.build_plan(
        parameters,
        readback,
        {"template_sha256": "t" * 64, "manifest_sha256": "m"},
        # A pre-apply world is planned against the pre-apply manifest (no
        # record); an applied world against the record the manifest now holds.
        applied=(
            (APPLIED if fake.stack_applied else None) if applied is None else applied
        ),
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
    assert containers["otel"] == source[1]
    app_env = evidence._env_map(containers["app"])
    app_secrets = evidence._secret_map(containers["app"])
    assert set(app_env) == evidence._BASE_ENV_NAMES | set(wiring.ADDED_ENVIRONMENT)
    assert set(app_secrets) == evidence._EXPECTED_APP_SECRET_NAMES | set(
        wiring.ADDED_SECRETS
    )
    assert app_env["BILLING_FACT_WORKER_ENABLED"] == "false"
    assert app_env["ASAAS_WEBHOOK_URL"] == (
        "https://staging.api.neurostack.global/v1/billing/webhook/asaas"
    )
    assert app_env["ASAAS_CHECKOUT_RETURN_URL"] == (
        "https://payments.staging.api.neurostack.global/billing"
    )
    assert app_env["ASAAS_WEBHOOK_NOTIFICATION_EMAIL"] == "neurostack.infra@gmail.com"
    assert {
        key: value for key, value in rendered.items() if key != "containerDefinitions"
    } == {
        key: value
        for key, value in wiring._request_from_response(_task_definition()).items()
        if key != "containerDefinitions"
    }
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


def test_structural_evidence_validators_pass_and_group5_allowlist_blocks() -> None:
    _plan_result, rendered = _plan(FakeAws(applied=True))
    assert rendered is not None
    results = wiring.run_evidence_validators(rendered, SOURCE_ARN)
    structural = [row for row in results if row["scope"] == "structural"]
    assert structural and all(row["result"] == "pass" for row in structural)
    (deploy_path,) = [row for row in results if row["scope"] == "group5_deploy_path"]
    # Known block F1: the protected deploy path pins revision 8's name sets.
    assert deploy_path == {
        "validator": "asaas_producer_rollout_evidence.validate_task_definition_template",
        "scope": "group5_deploy_path",
        "result": "fail",
        "reason": "task_definition_base_environment_names_mismatch",
    }


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


def test_rollback_target_is_revision_8() -> None:
    plan, _rendered = _plan(FakeAws(applied=True))
    assert plan["target"]["rollback_target"] == SOURCE_ARN
    assert SOURCE_ARN.endswith(":task-definition/neurostack-staging-app:8")
    assert _step(plan, "service_deployment")["info"]["rollback_target"] == SOURCE_ARN
    assert plan["task_definition_diff"]["source_task_definition_arn"] == SOURCE_ARN


def test_registered_revision_matching_the_rendering_is_compliant() -> None:
    fake = FakeAws(applied=True)
    _plan_result, rendered = _plan(fake)
    assert rendered is not None
    registered = _task_definition(NEXT_ARN)
    registered["taskDefinition"]["containerDefinitions"] = copy.deepcopy(
        rendered["containerDefinitions"]
    )
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
    assert manifest["target"]["rollback_task_definition_revision"] == 8
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
        lambda m: m["target"].__setitem__("rollback_task_definition_revision", 7),
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
    args = ["--params", str(PARAMS), "--readback-file", str(snapshot)]
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
    assert code == 2
    check = json.loads(out)
    assert check["structural_status"] == "pass"
    assert check["group5_deploy_path_status"] == "blocked"
    assert check["status"] == "blocked"


def test_render_refuses_before_the_secret_access_stack_exists(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = FakeAws().snapshot(tmp_path / "readback.json")
    code, out, err = _run(
        ["render-taskdef", "--params", str(PARAMS), "--readback-file", str(snapshot)],
        capsys,
    )
    assert code == 2
    assert out == ""
    # The manifest records step 1 as applied, so a missing stack is a block.
    assert "blocked: plan_blocked:applied_record_but_stack_absent" in err
    parameters = _validated()
    readback = wiring.collect_readback(
        wiring.ReadOnlyAws(FakeAws().transport), parameters
    )
    plan, rendered = wiring.build_plan(
        parameters, readback, {"template_sha256": "t" * 64}, applied=None
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
        wiring.collect_readback(wiring.ReadOnlyAws(transport), _validated())


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
    assert (
        "F1_group5_deploy_validator_allowlist:"
        "task_definition_base_environment_names_mismatch" in reasons
    )
    assert "F3_webhook_registration_entry_point:missing" in reasons
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
    args = ["--params", str(PARAMS), "--readback-file", str(snapshot)]
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


def test_path_b_fingerprint_and_unfingerprinted_d3_values_are_reported() -> None:
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
    assert info["d3_environment_values_fingerprinted"] is False
