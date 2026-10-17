from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from scripts import validate_asaas_producer_deploy_readback_policy as validator

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "infra/production/asaas-producer-deploy-readback-policy.yaml"


class CloudFormationLoader(yaml.SafeLoader):
    pass


CloudFormationLoader.add_multi_constructor(
    "!", lambda loader, tag, node: loader.construct_scalar(node)
)


def _policy() -> dict[str, object]:
    template = yaml.load(
        POLICY.read_text(encoding="utf-8"), Loader=CloudFormationLoader
    )
    return template["Resources"]["ProducerDeploymentReadbackPolicy"]["Properties"][
        "PolicyDocument"
    ]


def test_readback_policy_has_exact_read_only_action_allowlist() -> None:
    validator.validate_template(validator.load_template(POLICY))
    statements = _policy()["Statement"]
    actions = [action for statement in statements for action in statement["Action"]]
    assert set(actions) == {
        "ecs:DescribeServices",
        "ecr:DescribeImages",
        "secretsmanager:DescribeSecret",
        "secretsmanager:ListSecretVersionIds",
    }
    assert len(actions) == len(set(actions))
    assert all(statement["Effect"] == "Allow" for statement in statements)
    assert not any(action == "secretsmanager:GetSecretValue" for action in actions)
    assert not any(action.endswith(":*") or action == "*" for action in actions)
    assert all(statement["Resource"] != "*" for statement in statements)


def test_fargate_list_and_task_definition_reads_do_not_expand_resource_wildcards() -> (
    None
):
    actions = [action for row in _policy()["Statement"] for action in row["Action"]]
    assert "ecs:DescribeTasks" not in actions
    assert "ecs:ListTasks" not in actions
    assert "ecs:DescribeTaskDefinition" not in actions


def test_every_resource_scoped_read_is_pinned_to_its_target() -> None:
    statements = _policy()["Statement"]
    by_action = {
        statement["Action"][0]: statement
        for statement in statements
        if len(statement["Action"]) == 1
    }
    assert by_action["ecs:DescribeServices"]["Resource"] == [
        "arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging-app"
    ]
    assert by_action["ecr:DescribeImages"]["Resource"] == [{"Ref": "EcrRepositoryArn"}]
    secret_statement = next(
        row
        for row in statements
        if row["Sid"] == "ReadOnlyDatabaseSecretVersionMetadata"
    )
    assert secret_statement["Action"] == [
        "secretsmanager:DescribeSecret",
        "secretsmanager:ListSecretVersionIds",
    ]
    assert secret_statement["Resource"] == [{"Ref": "DatabaseSecretArn"}]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update({"Transform": "AWS::Serverless-2016-10-31"}),
        lambda value: value["Resources"]["ProducerDeploymentReadbackPolicy"][
            "Properties"
        ].update({"UnknownProperty": True}),
        lambda value: value["Resources"]["ProducerDeploymentReadbackPolicy"][
            "Properties"
        ]["PolicyDocument"]["Statement"][0].update({"Resource": ["*"]}),
        lambda value: value["Resources"]["ProducerDeploymentReadbackPolicy"][
            "Properties"
        ]["PolicyDocument"]["Statement"][0]["Action"].append("ecs:DescribeTasks"),
        lambda value: value["Resources"]["ProducerDeploymentReadbackPolicy"][
            "Properties"
        ]["PolicyDocument"]["Statement"][2]["Action"].append(
            "secretsmanager:GetSecretValue"
        ),
    ],
)
def test_group5_template_validator_rejects_unknown_keys_and_policy_expansion(
    mutate,
) -> None:
    template = copy.deepcopy(validator.load_template(POLICY))
    mutate(template)
    with pytest.raises(validator.PolicyTemplateError):
        validator.validate_template(template)
