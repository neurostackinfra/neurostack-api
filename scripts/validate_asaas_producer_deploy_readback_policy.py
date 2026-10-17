#!/usr/bin/env python3
"""Validate the Group 5 metadata-only IAM policy template."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "infra/production/asaas-producer-deploy-readback-policy.yaml"
SERVICE_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:service/"
    "neurostack-staging/neurostack-staging-app"
)
CLUSTER_ARN = "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging"

EXPECTED_ACTIONS = {
    "DescribeOnlyTheProductionServingService": ["ecs:DescribeServices"],
    "ReadImageDigestMetadataForOnlyTheProducerRepository": ["ecr:DescribeImages"],
    "ReadOnlyDatabaseSecretVersionMetadata": [
        "secretsmanager:DescribeSecret",
        "secretsmanager:ListSecretVersionIds",
    ],
}


class PolicyTemplateError(ValueError):
    """The Group 5 IAM template changed outside the reviewed schema."""


class CloudFormationLoader(yaml.SafeLoader):
    """Load the small set of CloudFormation intrinsics used by this template."""


def _ref(loader: CloudFormationLoader, node: yaml.Node) -> dict[str, str]:
    return {"Ref": loader.construct_scalar(node)}


CloudFormationLoader.add_constructor("!Ref", _ref)


def _exact_keys(value: Any, expected: set[str], location: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise PolicyTemplateError(f"{location}_keys_invalid")


def load_template(path: Path = DEFAULT_TEMPLATE) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise PolicyTemplateError("template_unreadable") from None
    if not isinstance(value, dict):
        raise PolicyTemplateError("template_shape_invalid")
    return value


def validate_template(template: Any) -> None:
    _exact_keys(
        template,
        {
            "AWSTemplateFormatVersion",
            "Description",
            "Metadata",
            "Parameters",
            "Resources",
        },
        "template",
    )
    if template["AWSTemplateFormatVersion"] != "2010-09-09":
        raise PolicyTemplateError("template_version_invalid")
    if not isinstance(template["Description"], str):
        raise PolicyTemplateError("template_description_invalid")

    _exact_keys(template["Metadata"], {"com.aws.cloudformation.Context"}, "metadata")
    metadata = template["Metadata"]["com.aws.cloudformation.Context"]
    _exact_keys(metadata, {"arch", "must"}, "metadata_context")
    if not isinstance(metadata["must"], list) or not metadata["must"]:
        raise PolicyTemplateError("metadata_requirements_invalid")

    parameters = template["Parameters"]
    expected_parameters = {
        "DeployRoleName": (
            "^neurostack-asaas-producer-release-DeployRole-[A-Za-z0-9]+$",
        ),
        "EcrRepositoryArn": (
            "^arn:aws:ecr:us-east-1:388460142431:repository/[A-Za-z0-9._/-]+$",
        ),
        "DatabaseSecretArn": (
            "^arn:aws:secretsmanager:us-east-1:388460142431:secret:[A-Za-z0-9/_+=.@-]+$",
        ),
    }
    _exact_keys(parameters, set(expected_parameters), "parameters")
    for name, (pattern,) in expected_parameters.items():
        parameter = parameters[name]
        _exact_keys(
            parameter,
            {"Type", "Description", "AllowedPattern", "ConstraintDescription"},
            f"parameter_{name}",
        )
        if (
            parameter["Type"] != "String"
            or not isinstance(parameter["Description"], str)
            or parameter["AllowedPattern"] != pattern
            or not isinstance(parameter["ConstraintDescription"], str)
        ):
            raise PolicyTemplateError(f"parameter_{name}_invalid")

    resources = template["Resources"]
    _exact_keys(resources, {"ProducerDeploymentReadbackPolicy"}, "resources")
    resource = resources["ProducerDeploymentReadbackPolicy"]
    _exact_keys(resource, {"Type", "Metadata", "Properties"}, "resource")
    if resource["Type"] != "AWS::IAM::Policy":
        raise PolicyTemplateError("resource_type_invalid")
    _exact_keys(
        resource["Metadata"], {"com.aws.cloudformation.Context"}, "resource_metadata"
    )
    resource_context = resource["Metadata"]["com.aws.cloudformation.Context"]
    _exact_keys(resource_context, {"why", "must"}, "resource_metadata_context")
    if not isinstance(resource_context["must"], list) or not resource_context["must"]:
        raise PolicyTemplateError("resource_metadata_requirements_invalid")

    properties = resource["Properties"]
    _exact_keys(properties, {"PolicyName", "Roles", "PolicyDocument"}, "properties")
    if properties["PolicyName"] != "asaas-producer-image-rollout-readback":
        raise PolicyTemplateError("policy_name_invalid")
    if properties["Roles"] != [{"Ref": "DeployRoleName"}]:
        raise PolicyTemplateError("policy_role_reference_invalid")

    document = properties["PolicyDocument"]
    _exact_keys(document, {"Version", "Statement"}, "policy_document")
    if document["Version"] != "2012-10-17":
        raise PolicyTemplateError("policy_version_invalid")
    statements = document["Statement"]
    if not isinstance(statements, list) or len(statements) != 3:
        raise PolicyTemplateError("policy_statements_invalid")
    by_sid: dict[str, dict[str, Any]] = {}
    for statement in statements:
        if not isinstance(statement, dict) or not isinstance(statement.get("Sid"), str):
            raise PolicyTemplateError("policy_statement_shape_invalid")
        sid = statement["Sid"]
        if sid in by_sid:
            raise PolicyTemplateError("policy_statement_sid_duplicate")
        by_sid[sid] = statement
    if set(by_sid) != set(EXPECTED_ACTIONS):
        raise PolicyTemplateError("policy_statement_set_invalid")

    for sid, actions in EXPECTED_ACTIONS.items():
        statement = by_sid[sid]
        expected_statement_keys = {"Sid", "Effect", "Action", "Resource"}
        if sid == "DescribeOnlyTheProductionServingService":
            expected_statement_keys.add("Condition")
        _exact_keys(statement, expected_statement_keys, f"statement_{sid}")
        if statement["Effect"] != "Allow" or statement["Action"] != actions:
            raise PolicyTemplateError(f"statement_{sid}_action_invalid")
        if not isinstance(statement["Resource"], list) or statement["Resource"] == [
            "*"
        ]:
            raise PolicyTemplateError(f"statement_{sid}_resource_invalid")
        if sid == "DescribeOnlyTheProductionServingService":
            if statement["Resource"] != [SERVICE_ARN] or statement["Condition"] != {
                "StringEquals": {"ecs:cluster": CLUSTER_ARN}
            }:
                raise PolicyTemplateError("service_read_scope_invalid")
        elif sid == "ReadImageDigestMetadataForOnlyTheProducerRepository":
            if statement["Resource"] != [{"Ref": "EcrRepositoryArn"}]:
                raise PolicyTemplateError("ecr_read_scope_invalid")
        elif statement["Resource"] != [{"Ref": "DatabaseSecretArn"}]:
            raise PolicyTemplateError("secret_metadata_read_scope_invalid")

    serialized_actions = [
        action for statement in statements for action in statement["Action"]
    ]
    if (
        "secretsmanager:GetSecretValue" in serialized_actions
        or any(action == "*" or action.endswith(":*") for action in serialized_actions)
        or any(statement["Resource"] == ["*"] for statement in statements)
    ):
        raise PolicyTemplateError("policy_scope_widened")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-template", action="store_true")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args(argv)
    try:
        if not args.check_template:
            parser.error("--check-template is required")
        validate_template(load_template(args.template))
        print("Group 5 deploy readback IAM template: valid")
        return 0
    except PolicyTemplateError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
