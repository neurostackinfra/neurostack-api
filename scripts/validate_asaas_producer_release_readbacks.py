#!/usr/bin/env python3
"""Fail-closed validator for sanitized ASAAS producer-release readbacks.

Usage:
  poetry run python scripts/validate_asaas_producer_release_readbacks.py --check-template
  poetry run python scripts/validate_asaas_producer_release_readbacks.py --evidence /secure/path/group-2-readbacks.json

The JSON evidence file is a normalized, secret-free snapshot assembled from
authoritative AWS and GitHub reads. Its exact schema is enforced by this module:

* `schema_version`, `collected_at_utc`, and `aws` / `github` are the only root
  fields. `collected_at_utc`, `aws.read_at_utc`, `github.read_at_utc`, and the
  DescribeRegions artifact timestamp are checked against current UTC time:
  each may be at most 24 hours old and at most five minutes in the future.
  `aws` contains the account/region,
  CloudFormation stack name/status/
  outputs, reused OIDC provider ARN/URL/client IDs, build/deploy/task roles,
  three KMS records, a complete account-wide KMS key-policy/grant inventory,
  ECR repository metadata, and `bootstrap_slot_ledger` controls. The ledger
  record must match the template's versioning, 100-year COMPLIANCE retention,
  encryption, public-access block, no-lifecycle state, and exact bucket policy.
  Its separate build/deployment `ListObjectVersions` page chains must terminate
  and contain no version or delete-marker items before initial bootstrap.
* Every role record contains `arn`, `assume_role_policy`, page records for
  `ListAttachedRolePolicies` and `ListRolePolicies`, a complete-document flag,
  and the complete `attached_managed_policies` / `inline_policies` values.
  Policy page records contain marker digests, item counts, and returned items;
  the terminal marker and concatenated page contents must match the complete
  values. Each inline policy is
  keyed by its AWS policy name and contains the complete sanitized IAM policy
  document. The task role includes every existing and newly attached inline
  policy.
* Each purpose key record contains ARN, state, spec, usage, signing algorithms,
  complete key policy, and `public_key` with `key_id`, base64 DER
  `public_key_der_base64`, canonical PEM, and `sha256:<hex>` fingerprint of the
  DER SubjectPublicKeyInfo bytes. Private key material must never be included.
* `kms_authorization_inventory` references a private local artifact containing
  the AWS MCP `DescribeRegions` envelope (`service_name`, `operation_name`,
  `region_name`, `request_params`, successful `api_calls`, the complete raw
  `response`, its canonical response SHA-256, and `read_at_utc`). The validator
  requires `AllRegions=true`, checks the response digest and API call count,
  derives enabled regions from the raw `Regions` records, and requires exactly
  one completed key/grant pagination record per enabled region. It also
  contains the inspected key count and all returned keys. Each key has `arn`, complete
  `key_policy`, and all grants with `grantee_principal` and `operations`. This
  lets the validator detect task-role signing paths outside the two approved
  runtime keys.
* The evidence JSON file's parent directory is the evidence root and must be
  outside this repository. Artifact paths are relative to that directory,
  cannot escape it (including through symlinks), and are verified by reading
  each file and recomputing SHA-256. The CLI never prints artifact contents.
* `github` contains repository identity, `/user` and repository permission
  readbacks for the UI attestor, the OIDC customization readback, ruleset
  page records bound to item IDs/counts and next-page cursors, each environment
  and deployment-branch-policy API readback, and independent UI evidence. The UI review artifact
  timestamp must be no more than five minutes before and no more than 24 hours
  after the GitHub readback. UI evidence points to a sanitized JSON attestation
  with a unique readback ID and verified SHA-256, plus three referenced PNG
  screenshot artifacts whose paths, structure, and SHA-256 values are also
  verified. Its claims are compared with the snapshot and canonical digests of
  the GitHub API readbacks and full snapshot, excluding the UI pointer to avoid
  a circular digest. This Group 2 setup record does not claim an Actions run.

Artifact paths are resolved relative to the `--evidence` file's parent. For
example, `--evidence /secure/group-2/snapshot.json` resolves
`aws/describe-regions.json` and `ui/attestation.json` under
`/secure/group-2/`. Keep that directory private and outside the repository.

The snapshot intentionally excludes credentials, tokens, private keys, and
unneeded user data. Validation errors report JSON paths and reason codes only;
they never echo snapshot values.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import hashlib
import json
import re
import stat
import struct
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "infra/production/asaas-producer-release.yaml"
PINNED_DESCRIBE_REGIONS_RESPONSE_SHA256 = (
    "f4e52f7420e339d831af2555ef035e75316cc01351296330ed68da7f119a6e00"
)

ACCOUNT_ID = "388460142431"
REGION = "us-east-1"
READBACK_SCHEMA_VERSION = "neurostack.asaas_producer_release_readbacks.v4"
MAX_EVIDENCE_AGE = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)
MAX_UI_AFTER_GITHUB = timedelta(hours=24)
MAX_ATTESTATION_BYTES = 1_048_576
MAX_UI_ARTIFACT_BYTES = 25 * 1024 * 1024
REPOSITORY = "neurostackinfra/neurostack-api"
REPOSITORY_ID = 1152335815
OWNER_ID = 340698182
RELEASE_REF = "refs/heads/main"
OIDC_PROVIDER_ARN = (
    "arn:aws:iam::388460142431:oidc-provider/token.actions.githubusercontent.com"
)
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
OIDC_PROVIDER_URL = "token.actions.githubusercontent.com"
OIDC_AUDIENCE = "sts.amazonaws.com"
TASK_ROLE_ARN = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-task"
EXECUTION_ROLE_ARN = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-execution"
TASK_DEFINITION_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:task-definition/" "neurostack-staging-app:*"
)
ECS_CLUSTER_ARN = "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging"
ECS_SERVICE_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:service/"
    "neurostack-staging/neurostack-staging-app"
)
BUILD_SUBJECT = (
    "repo:neurostackinfra@340698182/neurostack-api@1152335815:environment:"
    "asaas-build:ref:refs/heads/main:workflow_ref:"
    "neurostackinfra/neurostack-api/.github/workflows/"
    "asaas-build-attestation.yml@refs/heads/main"
)
DEPLOY_SUBJECT = (
    "repo:neurostackinfra@340698182/neurostack-api@1152335815:environment:"
    "asaas-production-deployment:ref:refs/heads/main:workflow_ref:"
    "neurostackinfra/neurostack-api/.github/workflows/"
    "asaas-producer-image-deploy.yml@refs/heads/main"
)
OIDC_SUBJECT_PREFIX = "repo:neurostackinfra@340698182/neurostack-api@1152335815"
OIDC_CLAIM_KEYS = ["repo", "context", "ref", "workflow_ref"]
ENVIRONMENTS = ("asaas-build", "asaas-production-deployment")
REPOSITORY_API_PATH = f"/repos/{REPOSITORY}"
AUTHENTICATED_USER_API_PATH = "/user"
OIDC_CUSTOMIZATION_API_PATH = f"{REPOSITORY_API_PATH}/actions/oidc/customization/sub"
RULESETS_API_PATH = f"{REPOSITORY_API_PATH}/rulesets"
KEY_IDS = (
    "BuildAttestationSigningKey",
    "ProducerGrantSigningKey",
    "ProducerReceiptSigningKey",
)
# Exact case-sensitive GrantOperation enum from the AWS KMS CreateGrant API.
# RevokeGrant is a separate API operation, not a grant operation value.
KMS_GRANT_OPERATIONS = frozenset(
    {
        "Decrypt",
        "Encrypt",
        "GenerateDataKey",
        "GenerateDataKeyWithoutPlaintext",
        "ReEncryptFrom",
        "ReEncryptTo",
        "Sign",
        "Verify",
        "GetPublicKey",
        "CreateGrant",
        "RetireGrant",
        "DescribeKey",
        "GenerateDataKeyPair",
        "GenerateDataKeyPairWithoutPlaintext",
        "GenerateMac",
        "VerifyMac",
        "DeriveSharedSecret",
    }
)
EXPECTED_RESOURCES = {
    "BootstrapSlotLedgerBucket": "AWS::S3::Bucket",
    "BootstrapSlotLedgerBucketPolicy": "AWS::S3::BucketPolicy",
    "ProducerImageRepository": "AWS::ECR::Repository",
    "BuildAttestationSigningKey": "AWS::KMS::Key",
    "ProducerGrantSigningKey": "AWS::KMS::Key",
    "ProducerReceiptSigningKey": "AWS::KMS::Key",
    "BuildRole": "AWS::IAM::Role",
    "DeployRole": "AWS::IAM::Role",
    "BuildRolePermissions": "AWS::IAM::Policy",
    "DeployRolePermissions": "AWS::IAM::Policy",
    "ExistingTaskRoleSigningPermissions": "AWS::IAM::Policy",
}
EXPECTED_OUTPUTS = {
    "BootstrapSlotLedgerBucketName",
    "GitHubOidcProviderArn",
    "ProducerImageRepositoryUri",
    "BuildRoleArn",
    "DeployRoleArn",
    "BuildAttestationSigningKeyArn",
    "ProducerGrantSigningKeyArn",
    "ProducerReceiptSigningKeyArn",
    "BuildOidcSubject",
    "DeployOidcSubject",
    "OidcSubjectClaimKeys",
    "OidcImmutableSubjectRequired",
}


class CloudFormationLoader(yaml.SafeLoader):
    """Load CloudFormation short tags as inert dictionaries."""


def _intrinsic(
    loader: CloudFormationLoader, suffix: str, node: yaml.Node
) -> dict[str, Any]:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    names = {"Ref": "Ref", "Sub": "Fn::Sub", "GetAtt": "Fn::GetAtt"}
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {names.get(suffix, f"Fn::{suffix}"): value}


CloudFormationLoader.add_multi_constructor("!", _intrinsic)


def _load_template(path: Path) -> dict[str, Any]:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=CloudFormationLoader)
    if not isinstance(loaded, dict):
        raise ValueError("template root is not a mapping")
    return loaded


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _loads_json_strict(content: str | bytes) -> Any:
    return json.loads(content, object_pairs_hook=_reject_duplicate_json_keys)


def _is_mapping(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _expect_mapping(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if not _is_mapping(value):
        errors.append(f"{path}: expected_object")
        return {}
    return value


def _expect_list(value: Any, path: str, errors: list[str]) -> list[Any]:
    if not isinstance(value, list):
        errors.append(f"{path}: expected_array")
        return []
    return value


def _exact_keys(
    value: Any, expected: set[str], path: str, errors: list[str]
) -> dict[str, Any]:
    mapping = _expect_mapping(value, path, errors)
    actual = set(mapping)
    for missing in sorted(expected - actual):
        errors.append(f"{path}.{missing}: missing_field")
    if actual - expected:
        errors.append(f"{path}: unexpected_field")
    return mapping


def _expect_equal(actual: Any, expected: Any, path: str, errors: list[str]) -> None:
    if actual != expected:
        errors.append(f"{path}: mismatch")


def _get(mapping: dict[str, Any], key: str, path: str, errors: list[str]) -> Any:
    if key not in mapping:
        errors.append(f"{path}.{key}: missing_field")
        return None
    return mapping[key]


def _role_trust(statement_id: str, subject: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": statement_id,
                "Effect": "Allow",
                "Principal": {"Federated": OIDC_PROVIDER_ARN},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        "token.actions.githubusercontent.com:aud": OIDC_AUDIENCE,
                        "token.actions.githubusercontent.com:sub": subject,
                    }
                },
            }
        ],
    }


def _task_role_trust() -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TasksDoEcsAssumemARole",
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT_ID}},
            }
        ],
    }


def _key_policy(sid: str, signer: Any) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AccountRootKeyAdministration",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT_ID}:root"},
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": sid,
                "Effect": "Allow",
                "Principal": {"AWS": signer},
                "Action": "kms:Sign",
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"kms:SigningAlgorithm": "ECDSA_SHA_256"}
                },
            },
        ],
    }


def _release_policy_properties(
    policy_name: Any, roles: list[Any], statements: list[Any]
) -> dict[str, Any]:
    return {
        "PolicyName": policy_name,
        "Roles": roles,
        "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": statements,
        },
    }


def _expected_release_outputs() -> dict[str, Any]:
    return {
        "BootstrapSlotLedgerBucketName": {"Ref": "BootstrapSlotLedgerBucket"},
        "GitHubOidcProviderArn": OIDC_PROVIDER_ARN,
        "ProducerImageRepositoryUri": {
            "Fn::GetAtt": ["ProducerImageRepository", "RepositoryUri"]
        },
        "BuildRoleArn": {"Fn::GetAtt": ["BuildRole", "Arn"]},
        "DeployRoleArn": {"Fn::GetAtt": ["DeployRole", "Arn"]},
        "BuildAttestationSigningKeyArn": {
            "Fn::GetAtt": ["BuildAttestationSigningKey", "Arn"]
        },
        "ProducerGrantSigningKeyArn": {
            "Fn::GetAtt": ["ProducerGrantSigningKey", "Arn"]
        },
        "ProducerReceiptSigningKeyArn": {
            "Fn::GetAtt": ["ProducerReceiptSigningKey", "Arn"]
        },
        "BuildOidcSubject": BUILD_SUBJECT,
        "DeployOidcSubject": DEPLOY_SUBJECT,
        "OidcSubjectClaimKeys": ",".join(OIDC_CLAIM_KEYS),
        "OidcImmutableSubjectRequired": "true",
    }


def _template_static_errors(template: Any) -> list[str]:
    errors: list[str] = []
    document = _expect_mapping(template, "template", errors)
    if set(document) != {
        "AWSTemplateFormatVersion",
        "Description",
        "Metadata",
        "Rules",
        "Resources",
        "Outputs",
    }:
        errors.append("template: top_level_field_set_mismatch")
    _expect_equal(
        document.get("AWSTemplateFormatVersion"),
        "2010-09-09",
        "template.AWSTemplateFormatVersion",
        errors,
    )
    resources = _expect_mapping(document.get("Resources"), "template.Resources", errors)
    if set(resources) != set(EXPECTED_RESOURCES):
        errors.append("template.Resources: resource_set_mismatch")
    for logical_id, resource_type in EXPECTED_RESOURCES.items():
        resource = resources.get(logical_id)
        if not _is_mapping(resource):
            errors.append(f"template.Resources.{logical_id}: missing_resource")
            continue
        allowed_fields = {"Type", "Metadata", "Properties"}
        if logical_id in {
            "BootstrapSlotLedgerBucket",
            "BootstrapSlotLedgerBucketPolicy",
            "ProducerImageRepository",
            "BuildAttestationSigningKey",
            "ProducerGrantSigningKey",
            "ProducerReceiptSigningKey",
        }:
            allowed_fields.update({"DeletionPolicy", "UpdateReplacePolicy"})
        if set(resource) - allowed_fields:
            errors.append(
                f"template.Resources.{logical_id}: unexpected_resource_fields"
            )
        _expect_equal(
            resource.get("Type"),
            resource_type,
            f"template.Resources.{logical_id}.Type",
            errors,
        )
    ledger_bucket = resources.get("BootstrapSlotLedgerBucket", {})
    ledger_bucket_properties = _expect_mapping(
        ledger_bucket.get("Properties") if _is_mapping(ledger_bucket) else None,
        "template.BootstrapSlotLedgerBucket.Properties",
        errors,
    )
    _expect_equal(
        ledger_bucket_properties,
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": [
                    {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                ]
            },
            "ObjectLockEnabled": True,
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Years": 100}},
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
            "VersioningConfiguration": {"Status": "Enabled"},
        },
        "template.BootstrapSlotLedgerBucket.Properties",
        errors,
    )
    if (
        ledger_bucket.get("DeletionPolicy") != "Retain"
        or ledger_bucket.get("UpdateReplacePolicy") != "Retain"
    ):
        errors.append("template.BootstrapSlotLedgerBucket: retention_policy_mismatch")

    build_claim_arn = {
        "Fn::Sub": "${BootstrapSlotLedgerBucket.Arn}/slots/build-bootstrap.json"
    }
    deployment_claim_arn = {
        "Fn::Sub": "${BootstrapSlotLedgerBucket.Arn}/slots/deployment-bootstrap.json"
    }
    expected_bucket_policy = {
        "Bucket": {"Ref": "BootstrapSlotLedgerBucket"},
        "PolicyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:*",
                    "Resource": [
                        {"Fn::GetAtt": ["BootstrapSlotLedgerBucket", "Arn"]},
                        build_claim_arn,
                        deployment_claim_arn,
                    ],
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
                {
                    "Sid": "RequireBuildClaimIfNoneMatchHeader",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:PutObject",
                    "Resource": build_claim_arn,
                    "Condition": {"Null": {"s3:if-none-match": "true"}},
                },
                {
                    "Sid": "RequireExactBuildClaimIfNoneMatchValue",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:PutObject",
                    "Resource": build_claim_arn,
                    "Condition": {"StringNotEquals": {"s3:if-none-match": "*"}},
                },
                {
                    "Sid": "RequireDeploymentClaimIfNoneMatchHeader",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:PutObject",
                    "Resource": deployment_claim_arn,
                    "Condition": {"Null": {"s3:if-none-match": "true"}},
                },
                {
                    "Sid": "RequireExactDeploymentClaimIfNoneMatchValue",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "s3:PutObject",
                    "Resource": deployment_claim_arn,
                    "Condition": {"StringNotEquals": {"s3:if-none-match": "*"}},
                },
                {
                    "Sid": "DenyBuildClaimDeletion",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                    "Resource": build_claim_arn,
                },
                {
                    "Sid": "DenyDeploymentClaimDeletion",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                    "Resource": deployment_claim_arn,
                },
            ],
        },
    }
    bucket_policy = resources.get("BootstrapSlotLedgerBucketPolicy", {})
    bucket_policy_properties = _expect_mapping(
        bucket_policy.get("Properties") if _is_mapping(bucket_policy) else None,
        "template.BootstrapSlotLedgerBucketPolicy.Properties",
        errors,
    )
    _expect_equal(
        bucket_policy_properties,
        expected_bucket_policy,
        "template.BootstrapSlotLedgerBucketPolicy.Properties",
        errors,
    )
    if (
        bucket_policy.get("DeletionPolicy") != "Retain"
        or bucket_policy.get("UpdateReplacePolicy") != "Retain"
    ):
        errors.append(
            "template.BootstrapSlotLedgerBucketPolicy: retention_policy_mismatch"
        )

    outputs = _expect_mapping(document.get("Outputs"), "template.Outputs", errors)
    if set(outputs) != EXPECTED_OUTPUTS:
        errors.append("template.Outputs: output_set_mismatch")
    for output_name, expected_value in _expected_release_outputs().items():
        output = outputs.get(output_name, {})
        _expect_equal(
            output.get("Value") if _is_mapping(output) else None,
            expected_value,
            f"template.Outputs.{output_name}.Value",
            errors,
        )

    repository = resources.get("ProducerImageRepository", {})
    repository_properties = _expect_mapping(
        repository.get("Properties") if _is_mapping(repository) else None,
        "template.ProducerImageRepository.Properties",
        errors,
    )
    _expect_equal(
        repository_properties,
        {
            "EncryptionConfiguration": {"EncryptionType": "AES256"},
            "ImageScanningConfiguration": {"ScanOnPush": True},
            "ImageTagMutability": "IMMUTABLE",
        },
        "template.ProducerImageRepository.Properties",
        errors,
    )
    if (
        repository.get("DeletionPolicy") != "Retain"
        or repository.get("UpdateReplacePolicy") != "Retain"
    ):
        errors.append("template.ProducerImageRepository: retention_policy_mismatch")

    build_subject = (
        outputs.get("BuildOidcSubject", {}).get("Value")
        if _is_mapping(outputs.get("BuildOidcSubject"))
        else None
    )
    deploy_subject = (
        outputs.get("DeployOidcSubject", {}).get("Value")
        if _is_mapping(outputs.get("DeployOidcSubject"))
        else None
    )
    _expect_equal(
        build_subject, BUILD_SUBJECT, "template.Outputs.BuildOidcSubject", errors
    )
    _expect_equal(
        deploy_subject, DEPLOY_SUBJECT, "template.Outputs.DeployOidcSubject", errors
    )
    rules = _expect_mapping(document.get("Rules"), "template.Rules", errors)
    if set(rules) != {"ReleaseAccountRegionBoundary"}:
        errors.append("template.Rules: rule_set_mismatch")
    release_rule = rules.get("ReleaseAccountRegionBoundary", {})
    assertions = release_rule.get("Assertions", []) if _is_mapping(release_rule) else []
    if (
        len(assertions) != 2
        or not all(_is_mapping(assertion) for assertion in assertions)
        or assertions[0].get("Assert")
        != {"Fn::Equals": [{"Ref": "AWS::AccountId"}, ACCOUNT_ID]}
        or assertions[1].get("Assert")
        != {"Fn::Equals": [{"Ref": "AWS::Region"}, REGION]}
    ):
        errors.append(
            "template.Rules.ReleaseAccountRegionBoundary: target_scope_mismatch"
        )

    for logical_id, statement_id, subject in (
        ("BuildRole", "ExactProtectedBuildWorkflow", BUILD_SUBJECT),
        ("DeployRole", "ExactProtectedDeploymentWorkflow", DEPLOY_SUBJECT),
    ):
        role = resources.get(logical_id, {})
        properties = _expect_mapping(
            role.get("Properties") if _is_mapping(role) else None,
            f"template.{logical_id}.Properties",
            errors,
        )
        expected = {
            "AssumeRolePolicyDocument": _role_trust(statement_id, subject),
            "MaxSessionDuration": 3600,
        }
        _expect_equal(properties, expected, f"template.{logical_id}.Properties", errors)

    expected_key_properties = {
        "BuildAttestationSigningKey": {
            "Description": "ASAAS producer build-attestation signing key",
            "Enabled": True,
            "KeySpec": "ECC_NIST_P256",
            "KeyUsage": "SIGN_VERIFY",
            "PendingWindowInDays": 30,
            "KeyPolicy": _key_policy(
                "BuildRoleAttestationSigning", {"Fn::GetAtt": ["BuildRole", "Arn"]}
            ),
        },
        "ProducerGrantSigningKey": {
            "Description": "ASAAS producer grant signing key",
            "Enabled": True,
            "KeySpec": "ECC_NIST_P256",
            "KeyUsage": "SIGN_VERIFY",
            "PendingWindowInDays": 30,
            "KeyPolicy": _key_policy("ProducerTaskRoleGrantSigning", TASK_ROLE_ARN),
        },
        "ProducerReceiptSigningKey": {
            "Description": "ASAAS producer receipt signing key",
            "Enabled": True,
            "KeySpec": "ECC_NIST_P256",
            "KeyUsage": "SIGN_VERIFY",
            "PendingWindowInDays": 30,
            "KeyPolicy": _key_policy("ProducerTaskRoleReceiptSigning", TASK_ROLE_ARN),
        },
    }
    for logical_id, expected_properties in expected_key_properties.items():
        resource = resources.get(logical_id, {})
        properties = _expect_mapping(
            resource.get("Properties") if _is_mapping(resource) else None,
            f"template.{logical_id}.Properties",
            errors,
        )
        _expect_equal(
            properties, expected_properties, f"template.{logical_id}.Properties", errors
        )
        if (
            properties.get("Enabled") is not True
            or properties.get("PendingWindowInDays") != 30
            or resource.get("DeletionPolicy") != "Retain"
            or resource.get("UpdateReplacePolicy") != "Retain"
        ):
            errors.append(f"template.{logical_id}: key_lifecycle_mismatch")

    build_policy = _release_policy_properties(
        {"Fn::Sub": "${AWS::StackName}-build"},
        [{"Ref": "BuildRole"}],
        [
            {
                "Sid": "EcrAuthorization",
                "Effect": "Allow",
                "Action": "ecr:GetAuthorizationToken",
                "Resource": "*",
            },
            {
                "Sid": "PublishAndReadOnlyTheProducerRepository",
                "Effect": "Allow",
                "Action": [
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:CompleteLayerUpload",
                    "ecr:DescribeImages",
                    "ecr:InitiateLayerUpload",
                    "ecr:PutImage",
                    "ecr:UploadLayerPart",
                ],
                "Resource": {"Fn::GetAtt": ["ProducerImageRepository", "Arn"]},
            },
            {
                "Sid": "SignOnlyTheBuildAttestationKey",
                "Effect": "Allow",
                "Action": "kms:Sign",
                "Resource": {"Fn::GetAtt": ["BuildAttestationSigningKey", "Arn"]},
                "Condition": {
                    "StringEquals": {"kms:SigningAlgorithm": "ECDSA_SHA_256"}
                },
            },
            {
                "Sid": "ReadOnlyTheBuildBootstrapLedgerControls",
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketVersioning",
                    "s3:GetBucketObjectLockConfiguration",
                    "s3:GetEncryptionConfiguration",
                    "s3:GetBucketPublicAccessBlock",
                    "s3:GetBucketPolicy",
                    "s3:GetLifecycleConfiguration",
                ],
                "Resource": {"Fn::GetAtt": ["BootstrapSlotLedgerBucket", "Arn"]},
            },
            {
                "Sid": "ListOnlyTheBuildBootstrapSlotVersions",
                "Effect": "Allow",
                "Action": "s3:ListBucketVersions",
                "Resource": {"Fn::GetAtt": ["BootstrapSlotLedgerBucket", "Arn"]},
                "Condition": {
                    "StringEquals": {"s3:prefix": "slots/build-bootstrap.json"}
                },
            },
            {
                "Sid": "CreateOnlyTheBuildBootstrapClaim",
                "Effect": "Allow",
                "Action": "s3:PutObject",
                "Resource": build_claim_arn,
            },
        ],
    )
    deploy_policy = _release_policy_properties(
        {"Fn::Sub": "${AWS::StackName}-deploy"},
        [{"Ref": "DeployRole"}],
        [
            {
                "Sid": "RegisterOnlyTheStagingTaskFamily",
                "Effect": "Allow",
                "Action": "ecs:RegisterTaskDefinition",
                "Resource": TASK_DEFINITION_ARN,
            },
            {
                "Sid": "UpdateOnlyTheStagingService",
                "Effect": "Allow",
                "Action": "ecs:UpdateService",
                "Resource": ECS_SERVICE_ARN,
                "Condition": {"StringEquals": {"ecs:cluster": ECS_CLUSTER_ARN}},
            },
            {
                "Sid": "PassOnlyTheExistingEcsTaskRoles",
                "Effect": "Allow",
                "Action": "iam:PassRole",
                "Resource": [TASK_ROLE_ARN, EXECUTION_ROLE_ARN],
                "Condition": {
                    "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                },
            },
            {
                "Sid": "ReadOnlyTheDeploymentBootstrapLedgerControls",
                "Effect": "Allow",
                "Action": [
                    "s3:GetBucketVersioning",
                    "s3:GetBucketObjectLockConfiguration",
                    "s3:GetEncryptionConfiguration",
                    "s3:GetBucketPublicAccessBlock",
                    "s3:GetBucketPolicy",
                    "s3:GetLifecycleConfiguration",
                ],
                "Resource": {"Fn::GetAtt": ["BootstrapSlotLedgerBucket", "Arn"]},
            },
            {
                "Sid": "ListOnlyTheDeploymentBootstrapSlotVersions",
                "Effect": "Allow",
                "Action": "s3:ListBucketVersions",
                "Resource": {"Fn::GetAtt": ["BootstrapSlotLedgerBucket", "Arn"]},
                "Condition": {
                    "StringEquals": {"s3:prefix": "slots/deployment-bootstrap.json"}
                },
            },
            {
                "Sid": "CreateOnlyTheDeploymentBootstrapClaim",
                "Effect": "Allow",
                "Action": "s3:PutObject",
                "Resource": deployment_claim_arn,
            },
        ],
    )
    task_signing_policy = _release_policy_properties(
        {"Fn::Sub": "${AWS::StackName}-task-signing"},
        ["neurostack-staging-ecs-task"],
        [
            {
                "Sid": "SignOnlyGrantAndReceiptKeys",
                "Effect": "Allow",
                "Action": "kms:Sign",
                "Resource": [
                    {"Fn::GetAtt": ["ProducerGrantSigningKey", "Arn"]},
                    {"Fn::GetAtt": ["ProducerReceiptSigningKey", "Arn"]},
                ],
                "Condition": {
                    "StringEquals": {"kms:SigningAlgorithm": "ECDSA_SHA_256"}
                },
            }
        ],
    )
    for logical_id, expected_properties in (
        ("BuildRolePermissions", build_policy),
        ("DeployRolePermissions", deploy_policy),
        ("ExistingTaskRoleSigningPermissions", task_signing_policy),
    ):
        resource = resources.get(logical_id, {})
        properties = _expect_mapping(
            resource.get("Properties") if _is_mapping(resource) else None,
            f"template.{logical_id}.Properties",
            errors,
        )
        _expect_equal(
            properties,
            expected_properties,
            f"template.{logical_id}.Properties",
            errors,
        )

    allowed_resources = ["*"] * 7 + [TASK_DEFINITION_ARN]
    actual_resources: list[str] = []

    def collect_resource_wildcards(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                collect_resource_wildcards(item)
        elif _is_mapping(value):
            for key, item in value.items():
                if key == "Resource":
                    if isinstance(item, str) and "*" in item:
                        actual_resources.append(item)
                    elif isinstance(item, list):
                        actual_resources.extend(
                            candidate
                            for candidate in item
                            if isinstance(candidate, str) and "*" in candidate
                        )
                else:
                    collect_resource_wildcards(item)

    collect_resource_wildcards(resources)
    _expect_equal(
        sorted(actual_resources),
        sorted(allowed_resources),
        "template.Resources.ResourceWildcards",
        errors,
    )

    for logical_id in ("BuildRole", "DeployRole"):
        role = resources.get(logical_id, {})
        properties = _expect_mapping(
            role.get("Properties") if _is_mapping(role) else None,
            f"template.{logical_id}.Properties",
            errors,
        )
        trust = properties.get("AssumeRolePolicyDocument", {})
        serialized = json.dumps(trust, sort_keys=True)
        if "*" in serialized:
            errors.append(
                f"template.{logical_id}.AssumeRolePolicyDocument: wildcard_trust"
            )
    return errors


def _resolve_template_value(
    value: Any,
    arns: dict[str, str],
    repository_uri: str,
    stack_name: str,
    bucket_name: str | None = None,
) -> Any:
    if isinstance(value, list):
        return [
            _resolve_template_value(item, arns, repository_uri, stack_name, bucket_name)
            for item in value
        ]
    if not _is_mapping(value):
        return value
    if set(value) == {"Ref"}:
        if value["Ref"] == "BootstrapSlotLedgerBucket":
            return bucket_name or "__unresolved_ref_BootstrapSlotLedgerBucket__"
        return value
    if set(value) == {"Fn::GetAtt"}:
        logical_id, attribute = value["Fn::GetAtt"]
        if logical_id == "ProducerImageRepository" and attribute == "RepositoryUri":
            return repository_uri
        if attribute == "Arn" and logical_id in arns:
            return arns[logical_id]
        return f"__unresolved_getatt_{logical_id}_{attribute}__"
    if set(value) == {"Fn::Sub"}:
        substitution = value["Fn::Sub"]
        variables: dict[str, Any] = {}
        if isinstance(substitution, list) and len(substitution) == 2:
            substitution, variables = substitution
        if not isinstance(substitution, str) or not _is_mapping(variables):
            return "__invalid_substitution__"

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in variables:
                resolved = _resolve_template_value(
                    variables[name], arns, repository_uri, stack_name, bucket_name
                )
                return str(resolved)
            if name == "AWS::StackName":
                return stack_name
            if name == "BootstrapSlotLedgerBucket" and bucket_name is not None:
                return bucket_name
            if name.endswith(".Arn") and name[:-4] in arns:
                return arns[name[:-4]]
            return f"__unresolved_sub_{name}__"

        return re.sub(r"\$\{([^}]+)\}", replace, substitution)
    return {
        key: _resolve_template_value(
            item, arns, repository_uri, stack_name, bucket_name
        )
        for key, item in value.items()
    }


def _is_nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(
        parsed
    )


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not _is_utc_timestamp(value):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _validate_fresh_timestamp(
    value: Any,
    path: str,
    now_utc: datetime,
    errors: list[str],
) -> datetime | None:
    timestamp = _parse_utc_timestamp(value)
    if timestamp is None:
        errors.append(f"{path}: invalid_utc_timestamp")
        return None
    age = now_utc - timestamp
    if age > MAX_EVIDENCE_AGE:
        errors.append(f"{path}: stale_evidence")
    elif age < -MAX_FUTURE_SKEW:
        errors.append(f"{path}: future_evidence")
    return timestamp


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _read_verified_artifact(
    value: Any,
    path: str,
    evidence_root: Path | None,
    errors: list[str],
    *,
    read_contents: bool = False,
    max_bytes: int = MAX_ATTESTATION_BYTES,
) -> bytes | None:
    descriptor = _exact_keys(value, {"path", "sha256"}, path, errors)
    relative_name = descriptor.get("path")
    expected_digest = descriptor.get("sha256")
    if (
        not isinstance(relative_name, str)
        or not relative_name
        or "\\" in relative_name
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", relative_name)
    ):
        errors.append(f"{path}.path: invalid_relative_artifact_path")
        return None
    relative_path = Path(relative_name)
    if (
        relative_path.is_absolute()
        or relative_path.as_posix() != relative_name
        or any(part in ("", ".", "..") for part in relative_path.parts)
    ):
        errors.append(f"{path}.path: invalid_relative_artifact_path")
        return None
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        errors.append(f"{path}.sha256: invalid_sha256")
        return None
    if evidence_root is None:
        errors.append(f"{path}: evidence_root_required")
        return None
    try:
        root_mode = evidence_root.lstat()
        root = evidence_root.resolve(strict=True)
        repository_root = ROOT.resolve(strict=True)
    except OSError:
        errors.append(f"{path}: evidence_root_unavailable")
        return None
    if stat.S_ISLNK(root_mode.st_mode):
        errors.append(f"{path}: evidence_root_symlink")
        return None
    if not root.is_dir():
        errors.append(f"{path}: evidence_root_not_directory")
        return None
    if root_mode.st_mode & 0o077:
        errors.append(f"{path}: evidence_root_permissions_open")
        return None
    if _is_within(root, repository_root):
        errors.append(f"{path}: evidence_root_inside_repository")
        return None
    artifact_path = root
    try:
        for index, part in enumerate(relative_path.parts):
            artifact_path = artifact_path / part
            artifact_mode = artifact_path.lstat()
            if stat.S_ISLNK(artifact_mode.st_mode):
                errors.append(f"{path}: artifact_symlink")
                return None
            is_last = index == len(relative_path.parts) - 1
            if is_last:
                if not stat.S_ISREG(artifact_mode.st_mode):
                    errors.append(f"{path}: artifact_not_regular_file")
                    return None
                if artifact_mode.st_mode & 0o077:
                    errors.append(f"{path}: artifact_permissions_open")
                    return None
                if artifact_mode.st_nlink != 1:
                    errors.append(f"{path}: artifact_hardlink")
                    return None
            else:
                if not stat.S_ISDIR(artifact_mode.st_mode):
                    errors.append(f"{path}: artifact_parent_not_directory")
                    return None
                if artifact_mode.st_mode & 0o077:
                    errors.append(f"{path}: artifact_directory_permissions_open")
                    return None
    except OSError:
        errors.append(f"{path}: artifact_missing_or_unreadable")
        return None
    if not _is_within(artifact_path.resolve(strict=True), root):
        errors.append(f"{path}: artifact_path_escape")
        return None
    try:
        size = artifact_path.stat().st_size
        if size == 0:
            errors.append(f"{path}: artifact_empty")
            return None
        if read_contents and size > max_bytes:
            errors.append(f"{path}: artifact_too_large")
            return None
        digest = hashlib.sha256()
        content = bytearray() if read_contents else None
        with artifact_path.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(64 * 1024), b""):
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
    except OSError:
        errors.append(f"{path}: artifact_missing_or_unreadable")
        return None
    if digest.hexdigest() != expected_digest:
        errors.append(f"{path}.sha256: artifact_digest_mismatch")
        return None
    return bytes(content) if content is not None else b""


def canonical_github_readback_sha256(github: Any) -> str | None:
    """Hash API-visible GitHub readbacks without the UI pointer's digest cycle."""
    if not _is_mapping(github):
        return None
    readback = {key: value for key, value in github.items() if key != "ui_evidence"}
    try:
        encoded = json.dumps(
            readback,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def canonical_snapshot_readback_sha256(snapshot: Any) -> str | None:
    """Hash AWS plus API-visible GitHub evidence, excluding the UI pointer."""
    if not _is_mapping(snapshot) or not _is_mapping(snapshot.get("github")):
        return None
    github = snapshot["github"]
    if "ui_evidence" not in github:
        return None
    github_readback = {
        key: value for key, value in github.items() if key != "ui_evidence"
    }
    payload = {key: value for key, value in snapshot.items() if key != "github"}
    payload["github"] = github_readback
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _validate_png(content: bytes, path: str, errors: list[str]) -> None:
    signature = b"\x89PNG\r\n\x1a\n"
    if not content.startswith(signature):
        errors.append(f"{path}: invalid_png_signature")
        return
    offset = len(signature)
    chunks: list[tuple[bytes, bytes]] = []
    saw_iend = False
    saw_idat = False
    idat_closed = False
    saw_plte = False
    known_critical_chunks = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
    while offset < len(content):
        if len(content) - offset < 12:
            errors.append(f"{path}: truncated_png_chunk")
            return
        chunk_length = int.from_bytes(content[offset : offset + 4], "big")
        chunk_type = content[offset + 4 : offset + 8]
        chunk_end = offset + 8 + chunk_length
        crc_end = chunk_end + 4
        if crc_end > len(content):
            errors.append(f"{path}: truncated_png_chunk")
            return
        chunk_data = content[offset + 8 : chunk_end]
        expected_crc = int.from_bytes(content[chunk_end:crc_end], "big")
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            errors.append(f"{path}: invalid_png_crc")
            return
        if any(
            byte not in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            for byte in chunk_type
        ):
            errors.append(f"{path}: invalid_png_chunk_type")
            return
        if chunk_type[2:3].islower():
            errors.append(f"{path}: invalid_png_reserved_chunk_bit")
            return
        if chunk_type[0:1].isupper() and chunk_type not in known_critical_chunks:
            errors.append(f"{path}: unknown_critical_png_chunk")
            return
        if not chunks and chunk_type != b"IHDR":
            errors.append(f"{path}: invalid_png_structure")
            return
        if chunks and chunk_type == b"IHDR":
            errors.append(f"{path}: duplicate_png_header")
            return
        if chunk_type == b"PLTE":
            if (
                saw_plte
                or saw_idat
                or not 3 <= chunk_length <= 768
                or chunk_length % 3 != 0
            ):
                errors.append(f"{path}: invalid_png_palette")
                return
            saw_plte = True
        if chunk_type == b"IDAT":
            if idat_closed:
                errors.append(f"{path}: nonconsecutive_png_image_data")
                return
            saw_idat = True
        elif saw_idat and chunk_type != b"IEND":
            idat_closed = True
        if chunk_type == b"IEND":
            if chunk_length != 0 or not saw_idat or offset + 12 != len(content):
                errors.append(f"{path}: invalid_png_end")
                return
            saw_iend = True
        chunks.append((chunk_type, chunk_data))
        offset = crc_end
        if chunk_type == b"IEND":
            break
    if not saw_iend or len(chunks) < 3:
        errors.append(f"{path}: incomplete_png")
        return
    if chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13 or chunks[-1][0] != b"IEND":
        errors.append(f"{path}: invalid_png_structure")
        return
    header = chunks[0][1]
    width, height, bit_depth, color_type, compression, filter_method, interlace = (
        struct.unpack(">IIBBBBB", header)
    )
    channels_by_color = {2: 3, 6: 4}
    if (
        width == 0
        or height == 0
        or width > 16384
        or height > 16384
        or width * height > 20_000_000
        or color_type not in channels_by_color
        or bit_depth != 8
        or compression != 0
        or filter_method != 0
        or interlace != 0
    ):
        errors.append(f"{path}: unsupported_png_header")
        return
    idat_chunks = [data for kind, data in chunks if kind == b"IDAT"]
    if not idat_chunks:
        errors.append(f"{path}: missing_png_image_data")
        return
    if saw_plte and color_type not in (2, 6):
        errors.append(f"{path}: unsupported_png_palette")
        return
    row_bytes = width * channels_by_color[color_type]
    expected_size = height * (row_bytes + 1)
    try:
        decompressor = zlib.decompressobj()
        image_data = decompressor.decompress(b"".join(idat_chunks), expected_size + 1)
    except zlib.error:
        errors.append(f"{path}: invalid_png_image_data")
        return
    if decompressor.unused_data or decompressor.unconsumed_tail or not decompressor.eof:
        errors.append(f"{path}: invalid_png_image_data")
        return
    if len(image_data) != expected_size:
        errors.append(f"{path}: png_image_data_size_mismatch")
        return
    for row in range(height):
        if image_data[row * (row_bytes + 1)] > 4:
            errors.append(f"{path}: invalid_png_filter")
            return


def _validate_describe_regions_artifact(
    value: Any,
    path: str,
    evidence_root: Path | None,
    now_utc: datetime,
    errors: list[str],
    expected_response_sha256: str | None = None,
) -> set[str]:
    content = _read_verified_artifact(
        value, path, evidence_root, errors, read_contents=True
    )
    if content is None:
        return set()
    try:
        artifact = _loads_json_strict(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        errors.append(f"{path}: invalid_describe_regions_json")
        return set()
    record = _exact_keys(
        artifact,
        {
            "schema_version",
            "source",
            "service_name",
            "operation_name",
            "region_name",
            "request_params",
            "api_calls",
            "read_at_utc",
            "response",
            "response_sha256",
        },
        f"{path}.document",
        errors,
    )
    _expect_equal(
        record.get("schema_version"),
        "neurostack.aws_ec2_describe_regions_readback.v1",
        f"{path}.document.schema_version",
        errors,
    )
    _expect_equal(
        record.get("source"),
        "aws_mcp__run_script.call_boto3",
        f"{path}.document.source",
        errors,
    )
    _expect_equal(
        record.get("service_name"), "ec2", f"{path}.document.service_name", errors
    )
    _expect_equal(
        record.get("operation_name"),
        "DescribeRegions",
        f"{path}.document.operation_name",
        errors,
    )
    _expect_equal(
        record.get("region_name"),
        "us-east-1",
        f"{path}.document.region_name",
        errors,
    )
    _expect_equal(
        record.get("request_params"),
        {"AllRegions": True},
        f"{path}.document.request_params",
        errors,
    )
    calls = _expect_list(record.get("api_calls"), f"{path}.document.api_calls", errors)
    _expect_equal(len(calls), 1, f"{path}.document.api_calls.count", errors)
    api_call = _expect_mapping(
        calls[0] if calls else {}, f"{path}.document.api_calls[0]", errors
    )
    _expect_equal(
        api_call.get("service"), "ec2", f"{path}.document.api_calls[0].service", errors
    )
    _expect_equal(
        api_call.get("operation"),
        "DescribeRegions",
        f"{path}.document.api_calls[0].operation",
        errors,
    )
    _expect_equal(
        api_call.get("status"),
        "success",
        f"{path}.document.api_calls[0].status",
        errors,
    )
    n_items = _expect_mapping(
        api_call.get("n_items"), f"{path}.document.api_calls[0].n_items", errors
    )
    response = _exact_keys(
        record.get("response"), {"Regions"}, f"{path}.document.response", errors
    )
    response_bytes = json.dumps(
        response, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    _expect_equal(
        record.get("response_sha256"),
        hashlib.sha256(response_bytes).hexdigest(),
        f"{path}.document.response_sha256",
        errors,
    )
    if expected_response_sha256 is not None:
        _expect_equal(
            record.get("response_sha256"),
            expected_response_sha256,
            f"{path}.document.response_sha256_pinned",
            errors,
        )
    _validate_fresh_timestamp(
        record.get("read_at_utc"), f"{path}.document.read_at_utc", now_utc, errors
    )
    raw_rows = _expect_list(
        response.get("Regions"), f"{path}.document.response.Regions", errors
    )
    _expect_equal(
        n_items,
        {"Regions": len(raw_rows)},
        f"{path}.document.api_calls[0].n_items",
        errors,
    )
    for index, raw_value in enumerate(raw_rows):
        raw_path = f"{path}.document.response.Regions[{index}]"
        raw_row = _expect_mapping(raw_value, raw_path, errors)
        name = raw_row.get("RegionName")
        status = raw_row.get("OptInStatus")
        if not isinstance(name, str) or not isinstance(status, str):
            errors.append(f"{raw_path}: missing_region_fields")
            continue
    if not raw_rows:
        errors.append(f"{path}.document.response.Regions: empty_response")
    all_regions: set[str] = set()
    enabled_regions: set[str] = set()
    for index, row_value in enumerate(raw_rows):
        row_path = f"{path}.document.regions[{index}]"
        row = _expect_mapping(row_value, row_path, errors)
        name = row.get("RegionName")
        status = row.get("OptInStatus")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", name) is None
        ):
            errors.append(f"{row_path}.region_name: invalid_region")
            continue
        if name in all_regions:
            errors.append(f"{row_path}.region_name: duplicate_region")
        all_regions.add(name)
        if status not in ("opt-in-not-required", "opted-in", "not-opted-in"):
            errors.append(f"{row_path}.opt_in_status: invalid_opt_in_status")
        elif status in ("opt-in-not-required", "opted-in"):
            enabled_regions.add(name)
    if not enabled_regions:
        errors.append(f"{path}.document.response.Regions: no_enabled_regions")
    return enabled_regions


def _validate_paged_items(
    value: Any,
    path: str,
    item_fields: set[str],
    errors: list[str],
) -> list[dict[str, Any]]:
    pages = _expect_list(value, path, errors)
    if not pages:
        errors.append(f"{path}: missing_pages")
        return []
    all_items: list[dict[str, Any]] = []
    expected_request_digest: str | None = None
    seen_markers: set[str] = set()
    terminated = False
    for index, page_value in enumerate(pages):
        page_path = f"{path}[{index}]"
        page = _exact_keys(
            page_value,
            {
                "request_marker_sha256",
                "next_marker_sha256",
                "truncated",
                "item_count",
                "items",
            },
            page_path,
            errors,
        )
        request_digest = page.get("request_marker_sha256")
        next_digest = page.get("next_marker_sha256")
        if request_digest != expected_request_digest:
            errors.append(f"{page_path}.request_marker_sha256: token_chain_mismatch")
        if request_digest is not None:
            if (
                not isinstance(request_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", request_digest) is None
            ):
                errors.append(
                    f"{page_path}.request_marker_sha256: invalid_token_digest"
                )
        items = _expect_list(page.get("items"), f"{page_path}.items", errors)
        if (
            isinstance(page.get("item_count"), bool)
            or not isinstance(page.get("item_count"), int)
            or page.get("item_count") != len(items)
        ):
            errors.append(f"{page_path}.item_count: returned_count_mismatch")
        for item_index, item_value in enumerate(items):
            item_path = f"{page_path}.items[{item_index}]"
            item = _exact_keys(item_value, item_fields, item_path, errors)
            all_items.append(item)
        truncated = page.get("truncated")
        if not isinstance(truncated, bool):
            errors.append(f"{page_path}.truncated: expected_boolean")
            truncated = False
        if truncated:
            if index == len(pages) - 1:
                errors.append(f"{page_path}: page_missing_after_next_marker")
            if (
                not isinstance(next_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", next_digest) is None
            ):
                errors.append(f"{page_path}.next_marker_sha256: required_token_digest")
                expected_request_digest = None
            elif next_digest in seen_markers:
                errors.append(f"{page_path}.next_marker_sha256: repeated_token_digest")
                expected_request_digest = next_digest
            else:
                seen_markers.add(next_digest)
                expected_request_digest = next_digest
        else:
            if next_digest is not None:
                errors.append(
                    f"{page_path}.next_marker_sha256: terminal_page_has_token"
                )
            if index != len(pages) - 1:
                errors.append(f"{page_path}: page_after_terminal_response")
            expected_request_digest = None
            terminated = True
    if not terminated:
        errors.append(f"{path}: terminal_page_required")
    return all_items


def _canonical_record_list(records: list[dict[str, Any]]) -> list[str]:
    return sorted(
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        for record in records
    )


def _validate_bootstrap_slot_ledger(
    value: Any,
    template: dict[str, Any],
    stack_name: str,
    now_utc: datetime,
    errors: list[str],
) -> str:
    path = "snapshot.aws.bootstrap_slot_ledger"
    ledger = _exact_keys(
        value,
        {
            "read_at_utc",
            "bucket_name",
            "versioning_status",
            "object_lock_configuration",
            "bucket_encryption",
            "public_access_block",
            "lifecycle_rules",
            "bucket_policy",
            "build_slot_versions_pages",
            "deployment_slot_versions_pages",
        },
        path,
        errors,
    )
    _validate_fresh_timestamp(
        ledger.get("read_at_utc"), f"{path}.read_at_utc", now_utc, errors
    )

    bucket_name = ledger.get("bucket_name")
    if (
        not isinstance(bucket_name, str)
        or not 3 <= len(bucket_name) <= 63
        or re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])", bucket_name) is None
        or ".." in bucket_name
        or ".-" in bucket_name
        or "-." in bucket_name
        or re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", bucket_name) is not None
    ):
        errors.append(f"{path}.bucket_name: invalid_bucket_name")
        bucket_name = "invalid-bootstrap-ledger-bucket"

    resources = _expect_mapping(template.get("Resources"), "template.Resources", errors)
    bucket = _expect_mapping(
        resources.get("BootstrapSlotLedgerBucket"),
        "template.BootstrapSlotLedgerBucket",
        errors,
    )
    properties = _expect_mapping(
        bucket.get("Properties"),
        "template.BootstrapSlotLedgerBucket.Properties",
        errors,
    )
    versioning = _expect_mapping(
        properties.get("VersioningConfiguration"),
        "template.BootstrapSlotLedgerBucket.Properties.VersioningConfiguration",
        errors,
    )
    _expect_equal(
        ledger.get("versioning_status"),
        versioning.get("Status"),
        f"{path}.versioning_status",
        errors,
    )
    lock_configuration = _expect_mapping(
        properties.get("ObjectLockConfiguration"),
        "template.BootstrapSlotLedgerBucket.Properties.ObjectLockConfiguration",
        errors,
    )
    lock_rule = _expect_mapping(
        lock_configuration.get("Rule"),
        "template.BootstrapSlotLedgerBucket.Properties.ObjectLockConfiguration.Rule",
        errors,
    )
    default_retention = _expect_mapping(
        lock_rule.get("DefaultRetention"),
        "template.BootstrapSlotLedgerBucket.Properties.ObjectLockConfiguration.Rule.DefaultRetention",
        errors,
    )
    _expect_equal(
        ledger.get("object_lock_configuration"),
        {
            "object_lock_enabled": lock_configuration.get("ObjectLockEnabled"),
            "default_retention_mode": default_retention.get("Mode"),
            "default_retention_years": default_retention.get("Years"),
        },
        f"{path}.object_lock_configuration",
        errors,
    )
    bucket_encryption = _expect_mapping(
        properties.get("BucketEncryption"),
        "template.BootstrapSlotLedgerBucket.Properties.BucketEncryption",
        errors,
    )
    encryption = _expect_list(
        bucket_encryption.get("ServerSideEncryptionConfiguration"),
        "template.BootstrapSlotLedgerBucket.Properties.BucketEncryption.ServerSideEncryptionConfiguration",
        errors,
    )
    encryption_algorithm = None
    if len(encryption) == 1 and _is_mapping(encryption[0]):
        encryption_default = _expect_mapping(
            encryption[0].get("ServerSideEncryptionByDefault"),
            "template.BootstrapSlotLedgerBucket.Properties.BucketEncryption.ServerSideEncryptionConfiguration[0].ServerSideEncryptionByDefault",
            errors,
        )
        encryption_algorithm = encryption_default.get("SSEAlgorithm")
    public_access_block = _expect_mapping(
        properties.get("PublicAccessBlockConfiguration"),
        "template.BootstrapSlotLedgerBucket.Properties.PublicAccessBlockConfiguration",
        errors,
    )
    _expect_equal(
        ledger.get("bucket_encryption"),
        {"sse_algorithm": encryption_algorithm},
        f"{path}.bucket_encryption",
        errors,
    )
    _expect_equal(
        ledger.get("public_access_block"),
        public_access_block,
        f"{path}.public_access_block",
        errors,
    )
    _expect_equal(
        ledger.get("lifecycle_rules"),
        [],
        f"{path}.lifecycle_rules",
        errors,
    )

    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    bucket_policy_resource = _expect_mapping(
        resources.get("BootstrapSlotLedgerBucketPolicy"),
        "template.BootstrapSlotLedgerBucketPolicy",
        errors,
    )
    bucket_policy_properties = _expect_mapping(
        bucket_policy_resource.get("Properties"),
        "template.BootstrapSlotLedgerBucketPolicy.Properties",
        errors,
    )
    expected_bucket_policy = _resolve_template_value(
        bucket_policy_properties.get("PolicyDocument"),
        {"BootstrapSlotLedgerBucket": bucket_arn},
        "",
        stack_name,
        bucket_name,
    )
    _expect_equal(
        ledger.get("bucket_policy"),
        expected_bucket_policy,
        f"{path}.bucket_policy",
        errors,
    )

    for slot_name, key in (
        ("build", "slots/build-bootstrap.json"),
        ("deployment", "slots/deployment-bootstrap.json"),
    ):
        items = _validate_paged_items(
            ledger.get(f"{slot_name}_slot_versions_pages"),
            f"{path}.{slot_name}_slot_versions_pages",
            {"key", "version_id", "kind"},
            errors,
        )
        for index, item in enumerate(items):
            item_path = f"{path}.{slot_name}_slot_versions_pages.items[{index}]"
            if item.get("key") != key:
                errors.append(f"{item_path}.key: unexpected_prefixed_key")
            if not _is_nonempty_string(item.get("version_id")):
                errors.append(f"{item_path}.version_id: invalid_version_id")
            if item.get("kind") not in ("version", "delete_marker"):
                errors.append(f"{item_path}.kind: invalid_version_record_kind")
        if items:
            errors.append(f"{path}.{slot_name}_slot_versions_pages: slot_not_empty")
    return bucket_name


def _validate_grant_operations(value: Any, path: str, errors: list[str]) -> list[str]:
    operations = _expect_list(value, path, errors)
    if not operations:
        errors.append(f"{path}: empty_grant_operations")
    if any(
        not isinstance(operation, str) or operation not in KMS_GRANT_OPERATIONS
        for operation in operations
    ):
        errors.append(f"{path}: unknown_or_case_mismatched_grant_operation")
    if len(operations) != len(
        set(operation for operation in operations if isinstance(operation, str))
    ):
        errors.append(f"{path}: duplicate_grant_operation")
    return operations


def _validate_public_key(
    public_key: Any, arn: str, path: str, errors: list[str]
) -> None:
    fields = _exact_keys(
        public_key,
        {"key_id", "public_key_der_base64", "public_key_pem", "sha256_fingerprint"},
        path,
        errors,
    )
    if fields.get("key_id") != arn:
        errors.append(f"{path}.key_id: key_identifier_mismatch")
    encoded = fields.get("public_key_der_base64")
    pem = fields.get("public_key_pem")
    fingerprint = fields.get("sha256_fingerprint")
    if not all(isinstance(item, str) for item in (encoded, pem, fingerprint)):
        errors.append(f"{path}: malformed_public_key_material")
        return
    try:
        der = base64.b64decode(encoded, validate=True)
        key = serialization.load_der_public_key(der)
        canonical_pem = key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
    except (ValueError, TypeError, binascii.Error):
        errors.append(f"{path}.public_key_der_base64: invalid_public_key_der")
        return
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        errors.append(f"{path}.public_key_der_base64: not_p256_public_key")
    if pem != canonical_pem:
        errors.append(f"{path}.public_key_pem: der_pem_mismatch")
    expected_fingerprint = f"sha256:{hashlib.sha256(der).hexdigest()}"
    if fingerprint != expected_fingerprint:
        errors.append(f"{path}.sha256_fingerprint: der_fingerprint_mismatch")


def _actions_allow_kms_sign(statement: dict[str, Any]) -> bool:
    if "Action" in statement:
        actions = statement["Action"]
        candidates = actions if isinstance(actions, list) else [actions]
        return any(
            isinstance(action, str) and fnmatch.fnmatchcase("kms:sign", action.lower())
            for action in candidates
        )
    if "NotAction" in statement:
        excluded = statement["NotAction"]
        candidates = excluded if isinstance(excluded, list) else [excluded]
        return not any(
            isinstance(action, str) and fnmatch.fnmatchcase("kms:sign", action.lower())
            for action in candidates
        )
    return False


def _statement_principals(statement: dict[str, Any]) -> list[str]:
    principal = statement.get("Principal", {})
    result: list[str] = []
    if principal == "*":
        return ["*"]
    if _is_mapping(principal):
        for value in principal.values():
            result.extend(value if isinstance(value, list) else [value])
    return [item for item in result if isinstance(item, str)]


def _is_task_role_principal(principal: str) -> bool:
    if principal == "*":
        return True
    session_prefix = (
        f"arn:aws:sts::{ACCOUNT_ID}:assumed-role/" "neurostack-staging-ecs-task/"
    )
    exact_session_arn = re.fullmatch(
        re.escape(session_prefix) + r"[^/]+",
        principal,
    )
    session_pattern_matches = fnmatch.fnmatchcase(
        f"{session_prefix}session-probe", principal
    )
    return (
        principal == TASK_ROLE_ARN
        or exact_session_arn is not None
        or fnmatch.fnmatchcase(TASK_ROLE_ARN, principal)
        or session_pattern_matches
    )


def _validate_inventory_key_policy(
    value: Any, path: str, errors: list[str]
) -> dict[str, Any]:
    policy = _expect_mapping(value, path, errors)
    required_fields = {"Version", "Statement"}
    allowed_fields = required_fields | {"Id"}
    for missing in sorted(required_fields - set(policy)):
        errors.append(f"{path}.{missing}: missing_field")
    if set(policy) - allowed_fields:
        errors.append(f"{path}: unexpected_field")
    if "Id" in policy and not _is_nonempty_string(policy["Id"]):
        errors.append(f"{path}.Id: invalid_policy_id")
    if policy.get("Version") != "2012-10-17":
        errors.append(f"{path}.Version: unsupported_policy_version")
    statements = _expect_list(policy.get("Statement"), f"{path}.Statement", errors)
    allowed_fields = {
        "Sid",
        "Effect",
        "Principal",
        "NotPrincipal",
        "Action",
        "NotAction",
        "Resource",
        "NotResource",
        "Condition",
    }
    for index, value_statement in enumerate(statements):
        statement_path = f"{path}.Statement[{index}]"
        statement = _expect_mapping(value_statement, statement_path, errors)
        if set(statement) - allowed_fields:
            errors.append(f"{statement_path}: unexpected_policy_fields")
        if statement.get("Effect") not in ("Allow", "Deny"):
            errors.append(f"{statement_path}.Effect: invalid_effect")
        if ("Action" in statement) == ("NotAction" in statement):
            errors.append(f"{statement_path}: action_selector_invalid")
        if ("Resource" in statement) == ("NotResource" in statement):
            errors.append(f"{statement_path}: resource_selector_invalid")
        if ("Principal" in statement) == ("NotPrincipal" in statement):
            errors.append(f"{statement_path}: principal_selector_invalid")
    if not statements:
        errors.append(f"{path}.Statement: empty_key_policy")
    return policy


def _validate_task_role_signing_surface(
    role: dict[str, Any],
    allowed_arns: set[str],
    task_signing_policy_name: str,
    path: str,
    errors: list[str],
) -> None:
    managed = _expect_list(
        role.get("attached_managed_policies"),
        f"{path}.attached_managed_policies",
        errors,
    )
    if managed:
        errors.append(f"{path}.attached_managed_policies: unexpected_managed_policy")
    policies = _expect_mapping(
        role.get("inline_policies"), f"{path}.inline_policies", errors
    )
    signing_policy_count = 0
    if len(policies) != 2:
        errors.append(f"{path}.inline_policies: expected_existing_and_release_policies")
    preexisting_kms_actions: set[str] = set()
    preexisting_kms_resources: set[str] = set()
    for policy_index, (policy_name, policy_document) in enumerate(policies.items()):
        policy_path = f"{path}.inline_policies[{policy_index}]"
        doc = _expect_mapping(policy_document, policy_path, errors)
        statements = _expect_list(
            doc.get("Statement"), f"{policy_path}.Statement", errors
        )
        for index, statement in enumerate(statements):
            if not _is_mapping(statement) or statement.get("Effect") != "Allow":
                continue
            actions_value = statement.get("Action", [])
            action_items = (
                actions_value if isinstance(actions_value, list) else [actions_value]
            )
            if policy_name != task_signing_policy_name:
                for action in action_items:
                    if isinstance(action, str) and (
                        action.lower().startswith("kms:") or action == "*"
                    ):
                        preexisting_kms_actions.add(action)
                        resources_value = statement.get("Resource", [])
                        resource_items = (
                            resources_value
                            if isinstance(resources_value, list)
                            else [resources_value]
                        )
                        preexisting_kms_resources.update(
                            resource
                            for resource in resource_items
                            if isinstance(resource, str)
                        )
            if not _actions_allow_kms_sign(statement):
                continue
            statement_path = f"{policy_path}.Statement[{index}]"
            actions = statement.get("Action")
            action_list = actions if isinstance(actions, list) else [actions]
            if action_list != ["kms:Sign"]:
                errors.append(f"{statement_path}: broad_kms_sign_action")
            resources = statement.get("Resource")
            resource_list = resources if isinstance(resources, list) else [resources]
            if not resource_list or any(
                item not in allowed_arns for item in resource_list
            ):
                errors.append(
                    f"{statement_path}: signing_resource_outside_allowed_keys"
                )
            if "NotResource" in statement:
                errors.append(f"{statement_path}: not_resource_is_not_permitted")
            if statement.get("Condition") != {
                "StringEquals": {"kms:SigningAlgorithm": "ECDSA_SHA_256"}
            }:
                errors.append(
                    f"{statement_path}.Condition: signing_algorithm_scope_mismatch"
                )
            signing_policy_count += 1
    if signing_policy_count != 1:
        errors.append(f"{path}.inline_policies: expected_single_signing_statement")
    if preexisting_kms_actions != {"kms:GenerateDataKey", "kms:Decrypt"}:
        errors.append(f"{path}.inline_policies: preflight_kms_actions_changed")
    if len(preexisting_kms_resources) != 1 or any(
        "*" in resource for resource in preexisting_kms_resources
    ):
        errors.append(f"{path}.inline_policies: preflight_kms_resource_not_exact")
    elif not re.fullmatch(
        rf"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}",
        next(iter(preexisting_kms_resources)),
    ):
        errors.append(f"{path}.inline_policies: preflight_kms_resource_not_account_key")


def _validate_ruleset(value: Any, path: str, errors: list[str]) -> None:
    readback = _exact_keys(
        value,
        {"endpoint", "http_status", "pages", "items"},
        path,
        errors,
    )
    if readback.get("endpoint") != RULESETS_API_PATH:
        errors.append(f"{path}.endpoint: unexpected_api_endpoint")
    if readback.get("http_status") != 200:
        errors.append(f"{path}.http_status: api_readback_failed")
    items = _expect_list(readback.get("items"), f"{path}.items", errors)
    pages = _expect_list(readback.get("pages"), f"{path}.pages", errors)
    if not pages:
        errors.append(f"{path}.pages: missing_pages")
    flattened_ids: list[Any] = []
    expected_page = 1
    terminal_seen = False
    for index, page_value in enumerate(pages):
        page_path = f"{path}.pages[{index}]"
        page = _exact_keys(
            page_value,
            {"request_page", "next_page", "item_count", "item_ids"},
            page_path,
            errors,
        )
        request_page = page.get("request_page")
        if isinstance(request_page, bool) or request_page != expected_page:
            errors.append(f"{page_path}.request_page: page_chain_mismatch")
        item_ids = _expect_list(page.get("item_ids"), f"{page_path}.item_ids", errors)
        if isinstance(page.get("item_count"), bool) or page.get("item_count") != len(
            item_ids
        ):
            errors.append(f"{page_path}.item_count: returned_count_mismatch")
        flattened_ids.extend(item_ids)
        next_page = page.get("next_page")
        if next_page is None:
            if index != len(pages) - 1:
                errors.append(f"{page_path}.next_page: terminal_page_not_last")
            terminal_seen = True
        elif (
            isinstance(next_page, bool)
            or not isinstance(next_page, int)
            or next_page != expected_page + 1
        ):
            errors.append(f"{page_path}.next_page: page_chain_mismatch")
        else:
            if index == len(pages) - 1:
                errors.append(f"{page_path}.next_page: page_missing_after_next_link")
            expected_page = next_page
    if pages and not terminal_seen:
        errors.append(f"{path}.pages: terminal_page_required")
    item_ids = [item.get("id") for item in items if isinstance(item, dict)]
    if len(item_ids) != len(items) or flattened_ids != item_ids:
        errors.append(f"{path}.pages: page_items_do_not_match_readback_items")
    if len(items) != 1:
        errors.append(f"{path}.items: expected_one_active_ruleset")
        return
    ruleset_path = f"{path}.items[0]"
    ruleset = _exact_keys(
        items[0],
        {"id", "name", "target", "enforcement", "conditions", "bypass_actors", "rules"},
        ruleset_path,
        errors,
    )
    if (
        isinstance(ruleset.get("id"), bool)
        or not isinstance(ruleset.get("id"), int)
        or ruleset.get("id") <= 0
    ):
        errors.append(f"{ruleset_path}.id: invalid_ruleset_id")
    if not _is_nonempty_string(ruleset.get("name")):
        errors.append(f"{ruleset_path}.name: required_name")
    if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
        errors.append(f"{ruleset_path}: not_active_branch_ruleset")
    conditions = _exact_keys(
        ruleset.get("conditions"), {"ref_name"}, f"{ruleset_path}.conditions", errors
    )
    ref_name = _exact_keys(
        conditions.get("ref_name"),
        {"include", "exclude"},
        f"{ruleset_path}.conditions.ref_name",
        errors,
    )
    if ref_name.get("include") != [RELEASE_REF] or ref_name.get("exclude") != []:
        errors.append(f"{ruleset_path}.conditions.ref_name: branch_scope_mismatch")
    if ruleset.get("bypass_actors") != []:
        errors.append(f"{ruleset_path}.bypass_actors: bypass_not_empty")
    rules = _expect_list(ruleset.get("rules"), f"{ruleset_path}.rules", errors)
    by_type: dict[str, dict[str, Any]] = {}
    for index, rule in enumerate(rules):
        item = _expect_mapping(rule, f"{ruleset_path}.rules[{index}]", errors)
        rule_type = item.get("type")
        if not isinstance(rule_type, str) or rule_type in by_type:
            errors.append(f"{ruleset_path}.rules[{index}]: duplicate_or_invalid_rule")
            continue
        by_type[rule_type] = item
    if set(by_type) != {
        "pull_request",
        "required_status_checks",
        "deletion",
        "non_fast_forward",
    }:
        errors.append(f"{ruleset_path}.rules: required_rule_set_mismatch")

    pull_request = by_type.get("pull_request", {})
    _exact_keys(
        pull_request,
        {"type", "parameters"},
        f"{ruleset_path}.rules.pull_request",
        errors,
    )
    pr_parameters = _exact_keys(
        pull_request.get("parameters"),
        {
            "required_approving_review_count",
            "dismiss_stale_reviews_on_push",
            "require_last_push_approval",
        },
        f"{ruleset_path}.rules.pull_request.parameters",
        errors,
    )
    approvals = pr_parameters.get("required_approving_review_count")
    if isinstance(approvals, bool) or not isinstance(approvals, int) or approvals < 1:
        errors.append(
            f"{ruleset_path}.rules.pull_request.parameters.required_approving_review_count: approval_required"
        )
    for field in ("dismiss_stale_reviews_on_push", "require_last_push_approval"):
        if pr_parameters.get(field) is not True:
            errors.append(
                f"{ruleset_path}.rules.pull_request.parameters.{field}: must_be_enabled"
            )

    checks_rule = by_type.get("required_status_checks", {})
    _exact_keys(
        checks_rule,
        {"type", "parameters"},
        f"{ruleset_path}.rules.required_status_checks",
        errors,
    )
    check_parameters = _exact_keys(
        checks_rule.get("parameters"),
        {"required_status_checks", "strict_required_status_checks_policy"},
        f"{ruleset_path}.rules.required_status_checks.parameters",
        errors,
    )
    if not isinstance(
        check_parameters.get("strict_required_status_checks_policy"), bool
    ):
        errors.append(
            f"{ruleset_path}.rules.required_status_checks.parameters.strict_required_status_checks_policy: expected_boolean"
        )
    required_checks = _expect_list(
        check_parameters.get("required_status_checks"),
        f"{ruleset_path}.rules.required_status_checks.parameters.required_status_checks",
        errors,
    )
    contexts: list[str] = []
    for index, check in enumerate(required_checks):
        check_path = f"{ruleset_path}.rules.required_status_checks.parameters.required_status_checks[{index}]"
        check_object = _exact_keys(
            check, {"context", "integration_id"}, check_path, errors
        )
        context = check_object.get("context")
        integration_id = check_object.get("integration_id")
        if not _is_nonempty_string(context):
            errors.append(f"{check_path}.context: required_named_check")
        elif context in contexts:
            errors.append(f"{check_path}.context: duplicate_check")
        else:
            contexts.append(context)
        if integration_id is not None and (
            isinstance(integration_id, bool)
            or not isinstance(integration_id, int)
            or integration_id <= 0
        ):
            errors.append(f"{check_path}.integration_id: invalid_integration_id")
    if not required_checks:
        errors.append(f"{ruleset_path}.rules.required_status_checks: no_named_checks")
    for rule_type in ("deletion", "non_fast_forward"):
        rule = by_type.get(rule_type, {})
        _exact_keys(rule, {"type"}, f"{ruleset_path}.rules.{rule_type}", errors)


def _validate_environment(
    value: Any, environment_name: str, path: str, errors: list[str]
) -> None:
    readback = _exact_keys(
        value,
        {
            "environment_endpoint",
            "environment_http_status",
            "environment",
            "branch_policies_endpoint",
            "branch_policies_http_status",
            "branch_policies",
        },
        path,
        errors,
    )
    expected_environment_path = f"{REPOSITORY_API_PATH}/environments/{environment_name}"
    if readback.get("environment_endpoint") != expected_environment_path:
        errors.append(f"{path}.environment_endpoint: unexpected_api_endpoint")
    if (
        readback.get("branch_policies_endpoint")
        != f"{expected_environment_path}/deployment-branch-policies"
    ):
        errors.append(f"{path}.branch_policies_endpoint: unexpected_api_endpoint")
    if readback.get("environment_http_status") != 200:
        errors.append(f"{path}.environment_http_status: api_readback_failed")
    environment = _exact_keys(
        readback.get("environment"),
        {"name", "can_admins_bypass", "protection_rules", "deployment_branch_policy"},
        f"{path}.environment",
        errors,
    )
    if environment.get("name") != environment_name:
        errors.append(f"{path}.environment.name: environment_name_mismatch")
    if environment.get("can_admins_bypass") is not False:
        errors.append(
            f"{path}.environment.can_admins_bypass: admin_bypass_must_be_disabled"
        )
    protection_rules = _expect_list(
        environment.get("protection_rules"),
        f"{path}.environment.protection_rules",
        errors,
    )
    if len(protection_rules) != 1:
        errors.append(
            f"{path}.environment.protection_rules: expected_required_reviewers_only"
        )
    reviewers: list[Any] = []
    prevent_self_review = False
    for index, rule in enumerate(protection_rules):
        rule_path = f"{path}.environment.protection_rules[{index}]"
        record = _exact_keys(
            rule, {"type", "reviewers", "prevent_self_review"}, rule_path, errors
        )
        if record.get("type") != "required_reviewers":
            errors.append(f"{rule_path}.type: required_reviewers_rule_missing")
        reviewers = _expect_list(
            record.get("reviewers"), f"{rule_path}.reviewers", errors
        )
        prevent_self_review = record.get("prevent_self_review") is True
        if record.get("prevent_self_review") is not True:
            errors.append(f"{rule_path}.prevent_self_review: must_be_enabled")
        reviewer_ids: set[tuple[str, int]] = set()
        for reviewer_index, reviewer in enumerate(reviewers):
            reviewer_path = f"{rule_path}.reviewers[{reviewer_index}]"
            reviewer_record = _exact_keys(
                reviewer,
                {"type", "id", "name"},
                reviewer_path,
                errors,
            )
            kind = reviewer_record.get("type")
            identifier = reviewer_record.get("id")
            if (
                kind not in ("User", "Team")
                or isinstance(identifier, bool)
                or not isinstance(identifier, int)
                or identifier <= 0
            ):
                errors.append(f"{reviewer_path}: invalid_reviewer_identity")
            elif (kind, identifier) in reviewer_ids:
                errors.append(f"{reviewer_path}: duplicate_reviewer")
            else:
                reviewer_ids.add((kind, identifier))
            if not _is_nonempty_string(reviewer_record.get("name")):
                errors.append(f"{reviewer_path}.name: missing_reviewer_name")
    if not reviewers:
        errors.append(f"{path}.environment.protection_rules: no_designated_reviewers")
    branch_policy = _exact_keys(
        environment.get("deployment_branch_policy"),
        {"protected_branches", "custom_branch_policies"},
        f"{path}.environment.deployment_branch_policy",
        errors,
    )
    if branch_policy != {"protected_branches": False, "custom_branch_policies": True}:
        errors.append(
            f"{path}.environment.deployment_branch_policy: custom_main_policy_required"
        )

    if readback.get("branch_policies_http_status") != 200:
        errors.append(f"{path}.branch_policies_http_status: api_readback_failed")
    policies = _exact_keys(
        readback.get("branch_policies"),
        {"total_count", "branch_policies"},
        f"{path}.branch_policies",
        errors,
    )
    branch_rows = _expect_list(
        policies.get("branch_policies"),
        f"{path}.branch_policies.branch_policies",
        errors,
    )
    if policies.get("total_count") != 1 or len(branch_rows) != 1:
        errors.append(f"{path}.branch_policies: expected_exactly_one_branch_policy")
    if branch_rows:
        row = _expect_mapping(
            branch_rows[0], f"{path}.branch_policies.branch_policies[0]", errors
        )
        if set(row) not in ({"name"}, {"name", "type"}):
            errors.append(
                f"{path}.branch_policies.branch_policies[0]: unexpected_policy_fields"
            )
        if row.get("name") != "main":
            errors.append(
                f"{path}.branch_policies.branch_policies[0].name: release_branch_mismatch"
            )
        if "type" in row and row.get("type") not in ("branch", "branch_name"):
            errors.append(
                f"{path}.branch_policies.branch_policies[0].type: tag_or_unknown_policy"
            )


def _validate_ui_evidence(
    value: Any,
    path: str,
    evidence_root: Path | None,
    now_utc: datetime,
    errors: list[str],
) -> dict[str, Any]:
    pointer = _exact_keys(value, {"attestation"}, path, errors)
    attestation_content = _read_verified_artifact(
        pointer.get("attestation"),
        f"{path}.attestation",
        evidence_root,
        errors,
        read_contents=True,
    )
    if attestation_content is None:
        return {}
    try:
        attestation_value = _loads_json_strict(attestation_content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        errors.append(f"{path}.attestation: invalid_attestation_json")
        return {}
    evidence = _exact_keys(
        attestation_value,
        {
            "schema_version",
            "readback_id",
            "reviewer",
            "checked_at_utc",
            "ref",
            "environments",
            "github_readback_sha256",
            "snapshot_readback_sha256",
            "artifacts",
            "ruleset_bypass_empty",
            "environment_admin_bypass_disabled",
            "environment_branch_policy_names",
            "environment_branch_only_no_tags",
        },
        f"{path}.attestation.document",
        errors,
    )
    if (
        evidence.get("schema_version")
        != "neurostack.asaas_producer_release_ui_attestation.v1"
    ):
        errors.append(f"{path}.attestation.document.schema_version: unsupported_schema")
    readback_id = evidence.get("readback_id")
    if (
        not isinstance(readback_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            readback_id,
        )
        is None
    ):
        errors.append(f"{path}.attestation.document.readback_id: invalid_readback_id")
    reviewer = _exact_keys(
        evidence.get("reviewer"),
        {"login", "id", "repository_role", "is_independent_repository_admin"},
        f"{path}.attestation.document.reviewer",
        errors,
    )
    if not _is_nonempty_string(reviewer.get("login")):
        errors.append(f"{path}.attestation.document.reviewer.login: required_reviewer")
    if (
        isinstance(reviewer.get("id"), bool)
        or not isinstance(reviewer.get("id"), int)
        or reviewer.get("id") <= 0
    ):
        errors.append(f"{path}.attestation.document.reviewer.id: invalid_reviewer_id")
    if reviewer.get("repository_role") != "admin":
        errors.append(
            f"{path}.attestation.document.reviewer.repository_role: repository_admin_required"
        )
    if reviewer.get("is_independent_repository_admin") is not True:
        errors.append(
            f"{path}.attestation.document.reviewer.is_independent_repository_admin: independent_admin_required"
        )
    checked_at = _validate_fresh_timestamp(
        evidence.get("checked_at_utc"),
        f"{path}.attestation.document.checked_at_utc",
        now_utc,
        errors,
    )
    _expect_equal(
        evidence.get("ref"), RELEASE_REF, f"{path}.attestation.document.ref", errors
    )
    _expect_equal(
        evidence.get("environments"),
        list(ENVIRONMENTS),
        f"{path}.attestation.document.environments",
        errors,
    )
    if (
        not isinstance(evidence.get("github_readback_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence.get("github_readback_sha256", ""))
        is None
    ):
        errors.append(
            f"{path}.attestation.document.github_readback_sha256: invalid_sha256"
        )
    if (
        not isinstance(evidence.get("snapshot_readback_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence.get("snapshot_readback_sha256", ""))
        is None
    ):
        errors.append(
            f"{path}.attestation.document.snapshot_readback_sha256: invalid_sha256"
        )

    artifacts = _exact_keys(
        evidence.get("artifacts"),
        {"ruleset", *ENVIRONMENTS},
        f"{path}.attestation.document.artifacts",
        errors,
    )
    artifact_paths: list[str] = []
    for artifact_name in ("ruleset", *ENVIRONMENTS):
        descriptor = artifacts.get(artifact_name)
        artifact_record = _exact_keys(
            descriptor,
            {"path", "sha256"},
            f"{path}.attestation.document.artifacts.{artifact_name}",
            errors,
        )
        if isinstance(artifact_record.get("path"), str):
            artifact_paths.append(artifact_record["path"])
        artifact_content = _read_verified_artifact(
            descriptor,
            f"{path}.attestation.document.artifacts.{artifact_name}",
            evidence_root,
            errors,
            read_contents=True,
            max_bytes=MAX_UI_ARTIFACT_BYTES,
        )
        relative_path = artifact_record.get("path")
        if not isinstance(relative_path, str) or not relative_path.lower().endswith(
            ".png"
        ):
            errors.append(
                f"{path}.attestation.document.artifacts.{artifact_name}.path: png_artifact_required"
            )
        if artifact_content is not None:
            _validate_png(
                artifact_content,
                f"{path}.attestation.document.artifacts.{artifact_name}",
                errors,
            )
    if len(artifact_paths) != len(set(artifact_paths)):
        errors.append(
            f"{path}.attestation.document.artifacts: distinct_artifacts_required"
        )

    if evidence.get("ruleset_bypass_empty") is not True:
        errors.append(
            f"{path}.attestation.document.ruleset_bypass_empty: ui_confirmation_required"
        )
    for field in (
        "environment_admin_bypass_disabled",
        "environment_branch_only_no_tags",
    ):
        settings = _exact_keys(
            evidence.get(field),
            set(ENVIRONMENTS),
            f"{path}.attestation.document.{field}",
            errors,
        )
        for environment_name in ENVIRONMENTS:
            if settings.get(environment_name) is not True:
                errors.append(
                    f"{path}.attestation.document.{field}.{environment_name}: ui_confirmation_required"
                )
    branch_names = _exact_keys(
        evidence.get("environment_branch_policy_names"),
        set(ENVIRONMENTS),
        f"{path}.attestation.document.environment_branch_policy_names",
        errors,
    )
    for environment_name in ENVIRONMENTS:
        if branch_names.get(environment_name) != ["main"]:
            errors.append(
                f"{path}.attestation.document.environment_branch_policy_names.{environment_name}: exact_branch_evidence_required"
            )
    if checked_at is None:
        return evidence
    return evidence


def validate_snapshot(
    snapshot: Any,
    template_path: Path = DEFAULT_TEMPLATE,
    evidence_root: Path | None = None,
    now_utc: datetime | None = None,
    expected_describe_regions_response_sha256: str | None = None,
) -> list[str]:
    """Return safe path-only failures; an empty list means every gate passed."""
    errors: list[str] = []
    now_utc = now_utc or datetime.now(timezone.utc)
    if now_utc.tzinfo is None or now_utc.utcoffset() != timezone.utc.utcoffset(now_utc):
        return ["validator.now_utc: invalid_utc_timestamp"]
    now_utc = now_utc.astimezone(timezone.utc)
    try:
        template = _load_template(template_path)
    except (OSError, yaml.YAMLError, ValueError):
        return ["template: unreadable_or_invalid"]
    errors.extend(_template_static_errors(template))
    root = _exact_keys(
        snapshot,
        {"schema_version", "collected_at_utc", "aws", "github"},
        "snapshot",
        errors,
    )
    if root.get("schema_version") != READBACK_SCHEMA_VERSION:
        errors.append("snapshot.schema_version: unsupported_schema")
    _validate_fresh_timestamp(
        root.get("collected_at_utc"), "snapshot.collected_at_utc", now_utc, errors
    )
    aws = _exact_keys(
        root.get("aws"),
        {
            "read_at_utc",
            "account_id",
            "region",
            "stack",
            "oidc_provider",
            "roles",
            "keys",
            "kms_authorization_inventory",
            "ecr",
            "bootstrap_slot_ledger",
        },
        "snapshot.aws",
        errors,
    )
    _validate_fresh_timestamp(
        aws.get("read_at_utc"), "snapshot.aws.read_at_utc", now_utc, errors
    )
    _expect_equal(aws.get("account_id"), ACCOUNT_ID, "snapshot.aws.account_id", errors)
    _expect_equal(aws.get("region"), REGION, "snapshot.aws.region", errors)

    stack = _exact_keys(
        aws.get("stack"), {"name", "status", "outputs"}, "snapshot.aws.stack", errors
    )
    stack_name = stack.get("name")
    if not _is_nonempty_string(stack_name) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9-]{0,127}", str(stack_name)
    ):
        errors.append("snapshot.aws.stack.name: invalid_stack_name")
        stack_name = "invalid-stack"
    if stack.get("status") not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
        errors.append("snapshot.aws.stack.status: stack_not_complete")
    bucket_name = _validate_bootstrap_slot_ledger(
        aws.get("bootstrap_slot_ledger"),
        template,
        str(stack_name),
        now_utc,
        errors,
    )

    provider = _exact_keys(
        aws.get("oidc_provider"),
        {"arn", "issuer", "url", "client_ids"},
        "snapshot.aws.oidc_provider",
        errors,
    )
    _expect_equal(
        provider.get("arn"), OIDC_PROVIDER_ARN, "snapshot.aws.oidc_provider.arn", errors
    )
    _expect_equal(
        provider.get("issuer"), OIDC_ISSUER, "snapshot.aws.oidc_provider.issuer", errors
    )
    _expect_equal(
        provider.get("url"), OIDC_PROVIDER_URL, "snapshot.aws.oidc_provider.url", errors
    )
    _expect_equal(
        provider.get("client_ids"),
        [OIDC_AUDIENCE],
        "snapshot.aws.oidc_provider.client_ids",
        errors,
    )

    roles = _exact_keys(
        aws.get("roles"),
        {"BuildRole", "DeployRole", "ExistingTaskRole"},
        "snapshot.aws.roles",
        errors,
    )
    role_arns: dict[str, str] = {}
    role_paths = {
        "BuildRole": "snapshot.aws.roles.BuildRole",
        "DeployRole": "snapshot.aws.roles.DeployRole",
        "ExistingTaskRole": "snapshot.aws.roles.ExistingTaskRole",
    }
    for logical_id, path in role_paths.items():
        role = _exact_keys(
            roles.get(logical_id),
            {
                "arn",
                "assume_role_policy",
                "attached_managed_policy_pages",
                "inline_policy_pages",
                "attached_managed_policies",
                "inline_policies",
            },
            path,
            errors,
        )
        arn = role.get("arn")
        if (
            not isinstance(arn, str)
            or not arn.startswith(f"arn:aws:iam::{ACCOUNT_ID}:role/")
            or "*" in arn
        ):
            errors.append(f"{path}.arn: invalid_role_arn")
        elif logical_id == "ExistingTaskRole" and arn != TASK_ROLE_ARN:
            errors.append(f"{path}.arn: fixed_task_role_mismatch")
        elif logical_id in ("BuildRole", "DeployRole"):
            role_arns[logical_id] = arn
        attached_pages = _validate_paged_items(
            role.get("attached_managed_policy_pages"),
            f"{path}.attached_managed_policy_pages",
            {"policy_arn", "policy_name"},
            errors,
        )
        attached_policies = _expect_list(
            role.get("attached_managed_policies"),
            f"{path}.attached_managed_policies",
            errors,
        )
        attached_records = [
            _exact_keys(
                policy,
                {"policy_arn", "policy_name"},
                f"{path}.attached_managed_policies[{index}]",
                errors,
            )
            for index, policy in enumerate(attached_policies)
        ]
        if attached_pages != attached_records:
            errors.append(
                f"{path}.attached_managed_policy_pages: page_items_do_not_match_complete_list"
            )
        if attached_records:
            errors.append(
                f"{path}.attached_managed_policies: unexpected_managed_policy"
            )
        inline_pages = _validate_paged_items(
            role.get("inline_policy_pages"),
            f"{path}.inline_policy_pages",
            {"policy_name"},
            errors,
        )
        inline_policies = _expect_mapping(
            role.get("inline_policies"), f"{path}.inline_policies", errors
        )
        page_names = [item.get("policy_name") for item in inline_pages]
        if any(not _is_nonempty_string(name) for name in page_names) or len(
            page_names
        ) != len(set(page_names)):
            errors.append(
                f"{path}.inline_policy_pages: invalid_or_duplicate_policy_name"
            )
        if set(page_names) != set(inline_policies):
            errors.append(
                f"{path}.inline_policy_pages: page_items_do_not_match_complete_list"
            )
    if len(role_arns) == 2 and len(set(role_arns.values())) != 2:
        errors.append("snapshot.aws.roles: build_and_deploy_roles_not_distinct")
    if TASK_ROLE_ARN in role_arns.values():
        errors.append("snapshot.aws.roles: release_role_matches_existing_task_role")

    keys = _exact_keys(aws.get("keys"), set(KEY_IDS), "snapshot.aws.keys", errors)
    key_arns: dict[str, str] = {}
    fingerprints: set[str] = set()
    for logical_id in KEY_IDS:
        path = f"snapshot.aws.keys.{logical_id}"
        key = _exact_keys(
            keys.get(logical_id),
            {
                "arn",
                "key_state",
                "key_spec",
                "key_usage",
                "signing_algorithms",
                "key_policy",
                "public_key",
            },
            path,
            errors,
        )
        arn = key.get("arn")
        if not isinstance(arn, str) or not re.fullmatch(
            rf"arn:aws:kms:{REGION}:{ACCOUNT_ID}:key/[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}",
            arn,
        ):
            errors.append(f"{path}.arn: invalid_key_arn")
        elif arn in key_arns.values():
            errors.append(f"{path}.arn: duplicate_key_arn")
        else:
            key_arns[logical_id] = arn
        _expect_equal(key.get("key_state"), "Enabled", f"{path}.key_state", errors)
        _expect_equal(key.get("key_spec"), "ECC_NIST_P256", f"{path}.key_spec", errors)
        _expect_equal(key.get("key_usage"), "SIGN_VERIFY", f"{path}.key_usage", errors)
        _expect_equal(
            key.get("signing_algorithms"),
            ["ECDSA_SHA_256"],
            f"{path}.signing_algorithms",
            errors,
        )
        fingerprint_before = (
            key.get("public_key", {}).get("sha256_fingerprint")
            if _is_mapping(key.get("public_key"))
            else None
        )
        _validate_public_key(
            key.get("public_key"),
            arn if isinstance(arn, str) else "",
            f"{path}.public_key",
            errors,
        )
        if isinstance(fingerprint_before, str):
            if fingerprint_before in fingerprints:
                errors.append(
                    f"{path}.public_key.sha256_fingerprint: duplicate_key_pin"
                )
            fingerprints.add(fingerprint_before)

    ecr = _exact_keys(
        aws.get("ecr"),
        {
            "repository_name",
            "repository_arn",
            "repository_uri",
            "image_tag_mutability",
            "encryption_type",
            "scan_on_push",
        },
        "snapshot.aws.ecr",
        errors,
    )
    repository_name = ecr.get("repository_name")
    if not isinstance(repository_name, str) or not re.fullmatch(
        r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository_name
    ):
        errors.append("snapshot.aws.ecr.repository_name: invalid_repository_name")
    expected_ecr_arn = (
        f"arn:aws:ecr:{REGION}:{ACCOUNT_ID}:repository/{repository_name}"
        if isinstance(repository_name, str)
        else ""
    )
    expected_ecr_uri_prefix = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/"
    _expect_equal(
        ecr.get("repository_arn"),
        expected_ecr_arn,
        "snapshot.aws.ecr.repository_arn",
        errors,
    )
    repository_uri = ecr.get("repository_uri")
    if repository_uri != f"{expected_ecr_uri_prefix}{repository_name}":
        errors.append("snapshot.aws.ecr.repository_uri: uri_mismatch")
    _expect_equal(
        ecr.get("image_tag_mutability"),
        "IMMUTABLE",
        "snapshot.aws.ecr.image_tag_mutability",
        errors,
    )
    _expect_equal(
        ecr.get("encryption_type"), "AES256", "snapshot.aws.ecr.encryption_type", errors
    )
    _expect_equal(
        ecr.get("scan_on_push"), True, "snapshot.aws.ecr.scan_on_push", errors
    )

    arns: dict[str, str] = {
        **role_arns,
        "ExistingTaskRole": TASK_ROLE_ARN,
        **key_arns,
        "ProducerImageRepository": expected_ecr_arn,
        "BootstrapSlotLedgerBucket": f"arn:aws:s3:::{bucket_name}",
    }
    try:
        expected_outputs: dict[str, Any] = {}
        for output_name, output in template.get("Outputs", {}).items():
            expected_outputs[output_name] = _resolve_template_value(
                output["Value"],
                arns,
                str(repository_uri),
                str(stack_name),
                bucket_name,
            )
    except (KeyError, TypeError, ValueError):
        errors.append("template.Outputs: cannot_resolve_output_contract")
        expected_outputs = {}
    outputs = _exact_keys(
        stack.get("outputs"),
        set(expected_outputs),
        "snapshot.aws.stack.outputs",
        errors,
    )
    for output_name, expected_value in expected_outputs.items():
        _expect_equal(
            outputs.get(output_name),
            expected_value,
            f"snapshot.aws.stack.outputs.{output_name}",
            errors,
        )

    # Compare all provisioned IAM trust documents and inline policies against the
    # template after resolving only the generated physical ARNs and stack name.
    for logical_id, expected_subject in (
        ("BuildRole", BUILD_SUBJECT),
        ("DeployRole", DEPLOY_SUBJECT),
    ):
        role = roles.get(logical_id, {}) if _is_mapping(roles) else {}
        role_path = role_paths[logical_id]
        template_properties = (
            template.get("Resources", {}).get(logical_id, {}).get("Properties", {})
        )
        expected_trust = template_properties.get("AssumeRolePolicyDocument")
        _expect_equal(
            role.get("assume_role_policy"),
            expected_trust,
            f"{role_path}.assume_role_policy",
            errors,
        )
        trust = role.get("assume_role_policy", {})
        if "*" in json.dumps(trust, sort_keys=True):
            errors.append(f"{role_path}.assume_role_policy: wildcard_trust")
        expected_policy_resource = (
            "BuildRolePermissions"
            if logical_id == "BuildRole"
            else "DeployRolePermissions"
        )
        template_policy = (
            template.get("Resources", {})
            .get(expected_policy_resource, {})
            .get("Properties", {})
        )
        policy_name = (
            f"{stack_name}-build"
            if logical_id == "BuildRole"
            else f"{stack_name}-deploy"
        )
        policy_map = role.get("inline_policies", {})
        if set(policy_map) != {policy_name}:
            errors.append(f"{role_path}.inline_policies: policy_name_set_mismatch")
        else:
            expected_document = _resolve_template_value(
                template_policy.get("PolicyDocument"),
                arns,
                str(repository_uri),
                str(stack_name),
            )
            label = "build-role" if logical_id == "BuildRole" else "deploy-role"
            _expect_equal(
                policy_map.get(policy_name),
                expected_document,
                f"{role_path}.inline_policies[{label}]",
                errors,
            )

    task_role = roles.get("ExistingTaskRole", {}) if _is_mapping(roles) else {}
    task_path = role_paths["ExistingTaskRole"]
    _expect_equal(
        task_role.get("assume_role_policy"),
        _task_role_trust(),
        f"{task_path}.assume_role_policy",
        errors,
    )
    task_policy_name = f"{stack_name}-task-signing"
    task_policy_map = task_role.get("inline_policies", {})
    expected_task_policy = _resolve_template_value(
        template.get("Resources", {})
        .get("ExistingTaskRoleSigningPermissions", {})
        .get("Properties", {})
        .get("PolicyDocument"),
        arns,
        str(repository_uri),
        str(stack_name),
    )
    if isinstance(task_policy_map, dict):
        if task_policy_map.get(task_policy_name) != expected_task_policy:
            errors.append(f"{task_path}.inline_policies[task-signing]: policy_mismatch")
    allowed_runtime_keys = {
        key_arns.get("ProducerGrantSigningKey", ""),
        key_arns.get("ProducerReceiptSigningKey", ""),
    }
    if "" in allowed_runtime_keys:
        errors.append(f"{task_path}.inline_policies: runtime_key_arns_missing")
    _validate_task_role_signing_surface(
        task_role, allowed_runtime_keys, task_policy_name, task_path, errors
    )

    for logical_id in KEY_IDS:
        key = keys.get(logical_id, {}) if _is_mapping(keys) else {}
        template_key = (
            template.get("Resources", {}).get(logical_id, {}).get("Properties", {})
        )
        expected_key_policy = _resolve_template_value(
            template_key.get("KeyPolicy"), arns, str(repository_uri), str(stack_name)
        )
        _expect_equal(
            key.get("key_policy"),
            expected_key_policy,
            f"snapshot.aws.keys.{logical_id}.key_policy",
            errors,
        )

    inventory = _exact_keys(
        aws.get("kms_authorization_inventory"),
        {
            "describe_regions_artifact",
            "regional_scans",
            "inspected_key_count",
            "keys",
        },
        "snapshot.aws.kms_authorization_inventory",
        errors,
    )
    inventory_path = "snapshot.aws.kms_authorization_inventory"
    regions = _validate_describe_regions_artifact(
        inventory.get("describe_regions_artifact"),
        f"{inventory_path}.describe_regions_artifact",
        evidence_root,
        now_utc,
        errors,
        expected_describe_regions_response_sha256,
    )
    regional_scans = _expect_mapping(
        inventory.get("regional_scans"), f"{inventory_path}.regional_scans", errors
    )
    if set(regional_scans) != regions:
        errors.append(
            f"{inventory_path}.regional_scans: enabled_region_coverage_mismatch"
        )
    regional_keys: dict[str, list[str]] = {}
    scanned_grants_by_arn: dict[str, list[dict[str, Any]]] = {}
    for region_name in sorted(regions):
        region_scan = _exact_keys(
            regional_scans.get(region_name),
            {"list_keys_pages", "key_grant_scans"},
            f"{inventory_path}.regional_scans.{region_name}",
            errors,
        )
        region_key_items = _validate_paged_items(
            region_scan.get("list_keys_pages"),
            f"{inventory_path}.regional_scans.{region_name}.list_keys_pages",
            {"arn"},
            errors,
        )
        region_key_arns: list[str] = []
        for key_index, key_item in enumerate(region_key_items):
            arn = key_item.get("arn")
            match = (
                re.fullmatch(
                    rf"arn:aws:kms:([a-z]{{2}}(?:-gov)?-[a-z]+-\d):{ACCOUNT_ID}:key/[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}",
                    arn,
                )
                if isinstance(arn, str)
                else None
            )
            if match is None:
                errors.append(
                    f"{inventory_path}.regional_scans.{region_name}.list_keys_pages.items[{key_index}].arn: invalid_account_kms_key_arn"
                )
            elif match.group(1) != region_name:
                errors.append(
                    f"{inventory_path}.regional_scans.{region_name}.list_keys_pages.items[{key_index}].arn: wrong_region"
                )
            region_key_arns.append(arn if isinstance(arn, str) else "")
        if len(region_key_arns) != len(set(region_key_arns)):
            errors.append(
                f"{inventory_path}.regional_scans.{region_name}.list_keys_pages: duplicate_key"
            )
        regional_keys[region_name] = region_key_arns
        key_grant_scans = _expect_mapping(
            region_scan.get("key_grant_scans"),
            f"{inventory_path}.regional_scans.{region_name}.key_grant_scans",
            errors,
        )
        if set(key_grant_scans) != set(region_key_arns):
            errors.append(
                f"{inventory_path}.regional_scans.{region_name}.key_grant_scans: key_coverage_mismatch"
            )
        for key_arn in region_key_arns:
            grant_scan = _exact_keys(
                key_grant_scans.get(key_arn),
                {"list_grants_pages"},
                f"{inventory_path}.regional_scans.{region_name}.key_grant_scans[{key_arn}]",
                errors,
            )
            grants = _validate_paged_items(
                grant_scan.get("list_grants_pages"),
                f"{inventory_path}.regional_scans.{region_name}.key_grant_scans[{key_arn}].list_grants_pages",
                {"grantee_principal", "operations"},
                errors,
            )
            for grant_index, grant in enumerate(grants):
                if not _is_nonempty_string(grant.get("grantee_principal")):
                    errors.append(
                        f"{inventory_path}.regional_scans.{region_name}.key_grant_scans[{key_arn}].list_grants_pages.items[{grant_index}].grantee_principal: invalid_principal"
                    )
                operations = _validate_grant_operations(
                    grant.get("operations"),
                    f"{inventory_path}.regional_scans.{region_name}.key_grant_scans[{key_arn}].list_grants_pages.items[{grant_index}].operations",
                    errors,
                )
            scanned_grants_by_arn[key_arn] = grants
    inventory_keys = _expect_list(
        inventory.get("keys"), "snapshot.aws.kms_authorization_inventory.keys", errors
    )
    if (
        inventory.get("inspected_key_count") != len(inventory_keys)
        or not inventory_keys
    ):
        errors.append(
            "snapshot.aws.kms_authorization_inventory.inspected_key_count: inventory_count_mismatch"
        )
    inventory_arns: set[str] = set()
    inventory_regions: dict[str, str] = {}
    inventory_grants_by_arn: dict[str, list[dict[str, Any]]] = {}
    allowed_task_sign_arns = allowed_runtime_keys
    provisioned_key_by_arn = {arn: logical_id for logical_id, arn in key_arns.items()}
    for index, item in enumerate(inventory_keys):
        path = f"snapshot.aws.kms_authorization_inventory.keys[{index}]"
        record = _exact_keys(item, {"arn", "key_policy", "grants"}, path, errors)
        key_arn = record.get("arn")
        match = (
            re.fullmatch(
                rf"arn:aws:kms:([a-z]{{2}}(?:-gov)?-[a-z]+-\d):{ACCOUNT_ID}:key/[0-9a-fA-F]{{8}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{4}}-[0-9a-fA-F]{{12}}",
                key_arn,
            )
            if isinstance(key_arn, str)
            else None
        )
        if match is None:
            errors.append(f"{path}.arn: invalid_account_kms_key_arn")
        elif match.group(1) not in regions:
            errors.append(f"{path}.arn: region_not_enabled_or_not_scanned")
        elif key_arn in inventory_arns:
            errors.append(f"{path}.arn: duplicate_inventory_key")
        else:
            inventory_arns.add(key_arn)
            inventory_regions[key_arn] = match.group(1)
        key_policy = _validate_inventory_key_policy(
            record.get("key_policy"), f"{path}.key_policy", errors
        )
        safe_key_arn = key_arn if isinstance(key_arn, str) else ""
        provisioned_logical_id = provisioned_key_by_arn.get(safe_key_arn)
        if provisioned_logical_id is not None:
            expected_policy = keys.get(provisioned_logical_id, {}).get("key_policy")
            _expect_equal(key_policy, expected_policy, f"{path}.key_policy", errors)
        grants = _expect_list(record.get("grants"), f"{path}.grants", errors)
        if provisioned_logical_id is not None and grants:
            errors.append(f"{path}.grants: unexpected_grant_on_release_key")
        for grant_index, grant in enumerate(grants):
            grant_path = f"{path}.grants[{grant_index}]"
            grant_record = _exact_keys(
                grant, {"grantee_principal", "operations"}, grant_path, errors
            )
            operations = _validate_grant_operations(
                grant_record.get("operations"), f"{grant_path}.operations", errors
            )
            if not _is_nonempty_string(grant_record.get("grantee_principal")):
                errors.append(f"{grant_path}.grantee_principal: invalid_principal")
            inventory_grants_by_arn.setdefault(safe_key_arn, []).append(grant_record)
            if (
                isinstance(grant_record.get("grantee_principal"), str)
                and _is_task_role_principal(grant_record["grantee_principal"])
                and "Sign" in operations
                and safe_key_arn not in allowed_task_sign_arns
            ):
                errors.append(
                    f"{grant_path}: task_role_sign_grant_outside_runtime_keys"
                )
    scanned_arns = {
        key_arn
        for region_key_arns in regional_keys.values()
        for key_arn in region_key_arns
    }
    if scanned_arns != inventory_arns:
        errors.append(f"{inventory_path}.regional_scans: list_keys_inventory_mismatch")
    for region_name, key_arns_in_region in regional_keys.items():
        expected_region_arns = {
            key_arn
            for key_arn, key_region in inventory_regions.items()
            if key_region == region_name
        }
        if set(key_arns_in_region) != expected_region_arns:
            errors.append(
                f"{inventory_path}.regional_scans.{region_name}.list_keys_pages: key_inventory_mismatch"
            )
    for key_arn in inventory_arns | set(scanned_grants_by_arn):
        if _canonical_record_list(
            inventory_grants_by_arn.get(key_arn, [])
        ) != _canonical_record_list(scanned_grants_by_arn.get(key_arn, [])):
            errors.append(
                f"{inventory_path}.regional_scans: list_grants_inventory_mismatch"
            )
    if not set(key_arns.values()).issubset(inventory_arns):
        errors.append(
            "snapshot.aws.kms_authorization_inventory.keys: missing_provisioned_key"
        )

    # Scan every key policy in the complete KMS inventory for a direct task-role
    # signing allow outside the exact grant/receipt keys.
    for index, item in enumerate(inventory_keys):
        if not _is_mapping(item):
            continue
        key_arn = item.get("arn")
        safe_key_arn = key_arn if isinstance(key_arn, str) else ""
        key_policy = item.get("key_policy", {})
        statements = key_policy.get("Statement", []) if _is_mapping(key_policy) else []
        if not isinstance(statements, list):
            errors.append(
                f"snapshot.aws.kms_authorization_inventory.keys[{index}].key_policy.Statement: expected_array"
            )
            continue
        for statement_index, statement in enumerate(statements):
            if (
                not _is_mapping(statement)
                or statement.get("Effect") != "Allow"
                or not _actions_allow_kms_sign(statement)
            ):
                continue
            principals = _statement_principals(statement)
            if "NotPrincipal" in statement or "NotResource" in statement:
                errors.append(
                    f"snapshot.aws.kms_authorization_inventory.keys[{index}].key_policy.Statement[{statement_index}]: unsupported_broad_sign_policy"
                )
                continue
            if (
                any(_is_task_role_principal(principal) for principal in principals)
                or "*" in principals
            ) and safe_key_arn not in allowed_task_sign_arns:
                errors.append(
                    f"snapshot.aws.kms_authorization_inventory.keys[{index}].key_policy.Statement[{statement_index}]: task_role_sign_path_outside_runtime_keys"
                )

    github = _exact_keys(
        root.get("github"),
        {
            "read_at_utc",
            "repository",
            "attestor_identity",
            "attestor_repository_permission",
            "oidc_subject_customization",
            "rulesets",
            "environments",
            "ui_evidence",
        },
        "snapshot.github",
        errors,
    )
    github_read_at = _validate_fresh_timestamp(
        github.get("read_at_utc"), "snapshot.github.read_at_utc", now_utc, errors
    )
    repository = _exact_keys(
        github.get("repository"),
        {"endpoint", "http_status", "full_name", "id", "owner_id", "default_branch"},
        "snapshot.github.repository",
        errors,
    )
    _expect_equal(
        repository.get("endpoint"),
        REPOSITORY_API_PATH,
        "snapshot.github.repository.endpoint",
        errors,
    )
    _expect_equal(
        repository.get("http_status"),
        200,
        "snapshot.github.repository.http_status",
        errors,
    )
    _expect_equal(
        repository.get("full_name"),
        REPOSITORY,
        "snapshot.github.repository.full_name",
        errors,
    )
    _expect_equal(
        repository.get("id"), REPOSITORY_ID, "snapshot.github.repository.id", errors
    )
    _expect_equal(
        repository.get("owner_id"),
        OWNER_ID,
        "snapshot.github.repository.owner_id",
        errors,
    )
    _expect_equal(
        repository.get("default_branch"),
        "main",
        "snapshot.github.repository.default_branch",
        errors,
    )

    attestor_identity = _exact_keys(
        github.get("attestor_identity"),
        {"endpoint", "http_status", "login", "id"},
        "snapshot.github.attestor_identity",
        errors,
    )
    _expect_equal(
        attestor_identity.get("endpoint"),
        AUTHENTICATED_USER_API_PATH,
        "snapshot.github.attestor_identity.endpoint",
        errors,
    )
    _expect_equal(
        attestor_identity.get("http_status"),
        200,
        "snapshot.github.attestor_identity.http_status",
        errors,
    )
    if not _is_nonempty_string(attestor_identity.get("login")):
        errors.append("snapshot.github.attestor_identity.login: required_reviewer")
    if (
        isinstance(attestor_identity.get("id"), bool)
        or not isinstance(attestor_identity.get("id"), int)
        or attestor_identity.get("id") <= 0
    ):
        errors.append("snapshot.github.attestor_identity.id: invalid_reviewer_id")
    permission = _exact_keys(
        github.get("attestor_repository_permission"),
        {"endpoint", "http_status", "login", "id", "permission"},
        "snapshot.github.attestor_repository_permission",
        errors,
    )
    login = attestor_identity.get("login")
    expected_permission_endpoint = (
        f"{REPOSITORY_API_PATH}/collaborators/{login}/permission"
        if isinstance(login, str)
        else ""
    )
    _expect_equal(
        permission.get("endpoint"),
        expected_permission_endpoint,
        "snapshot.github.attestor_repository_permission.endpoint",
        errors,
    )
    _expect_equal(
        permission.get("http_status"),
        200,
        "snapshot.github.attestor_repository_permission.http_status",
        errors,
    )
    _expect_equal(
        permission.get("login"),
        login,
        "snapshot.github.attestor_repository_permission.login",
        errors,
    )
    _expect_equal(
        permission.get("id"),
        attestor_identity.get("id"),
        "snapshot.github.attestor_repository_permission.id",
        errors,
    )
    _expect_equal(
        permission.get("permission"),
        "admin",
        "snapshot.github.attestor_repository_permission.permission",
        errors,
    )

    oidc = _exact_keys(
        github.get("oidc_subject_customization"),
        {
            "endpoint",
            "http_status",
            "use_default",
            "use_immutable_subject",
            "sub_claim_prefix",
            "include_claim_keys",
        },
        "snapshot.github.oidc_subject_customization",
        errors,
    )
    _expect_equal(
        oidc.get("endpoint"),
        OIDC_CUSTOMIZATION_API_PATH,
        "snapshot.github.oidc_subject_customization.endpoint",
        errors,
    )
    _expect_equal(
        oidc.get("http_status"),
        200,
        "snapshot.github.oidc_subject_customization.http_status",
        errors,
    )
    _expect_equal(
        oidc.get("use_default"),
        False,
        "snapshot.github.oidc_subject_customization.use_default",
        errors,
    )
    _expect_equal(
        oidc.get("use_immutable_subject"),
        True,
        "snapshot.github.oidc_subject_customization.use_immutable_subject",
        errors,
    )
    _expect_equal(
        oidc.get("sub_claim_prefix"),
        OIDC_SUBJECT_PREFIX,
        "snapshot.github.oidc_subject_customization.sub_claim_prefix",
        errors,
    )
    _expect_equal(
        oidc.get("include_claim_keys"),
        OIDC_CLAIM_KEYS,
        "snapshot.github.oidc_subject_customization.include_claim_keys",
        errors,
    )

    _validate_ruleset(github.get("rulesets"), "snapshot.github.rulesets", errors)
    environments = _exact_keys(
        github.get("environments"),
        set(ENVIRONMENTS),
        "snapshot.github.environments",
        errors,
    )
    for environment_name in ENVIRONMENTS:
        _validate_environment(
            environments.get(environment_name),
            environment_name,
            f"snapshot.github.environments.{environment_name}",
            errors,
        )
    ui_evidence = _validate_ui_evidence(
        github.get("ui_evidence"),
        "snapshot.github.ui_evidence",
        evidence_root,
        now_utc,
        errors,
    )
    reviewer = ui_evidence.get("reviewer", {}) if isinstance(ui_evidence, dict) else {}
    _expect_equal(
        reviewer.get("login"),
        attestor_identity.get("login"),
        "snapshot.github.ui_evidence.attestation.reviewer.login: identity_readback_mismatch",
        errors,
    )
    _expect_equal(
        reviewer.get("id"),
        attestor_identity.get("id"),
        "snapshot.github.ui_evidence.attestation.reviewer.id: identity_readback_mismatch",
        errors,
    )
    _expect_equal(
        reviewer.get("repository_role"),
        permission.get("permission"),
        "snapshot.github.ui_evidence.attestation.reviewer.repository_role: permission_readback_mismatch",
        errors,
    )
    if permission.get("permission") != "admin":
        errors.append(
            "snapshot.github.ui_evidence.attestation.reviewer.repository_role: repository_admin_required"
        )
    api_branch_names: dict[str, list[str]] = {}
    for environment_name in ENVIRONMENTS:
        environment_readback = environments.get(environment_name, {})
        branch_policies = (
            environment_readback.get("branch_policies", {})
            if _is_mapping(environment_readback)
            else {}
        )
        rows = (
            branch_policies.get("branch_policies", [])
            if _is_mapping(branch_policies)
            else []
        )
        api_branch_names[environment_name] = (
            [row.get("name") for row in rows if _is_mapping(row)]
            if isinstance(rows, list)
            else []
        )
    ui_branch_names = ui_evidence.get("environment_branch_policy_names", {})
    _expect_equal(
        ui_branch_names,
        api_branch_names,
        "snapshot.github.ui_evidence.attestation.document.environment_branch_policy_names",
        errors,
    )
    checked_at = (
        _parse_utc_timestamp(ui_evidence.get("checked_at_utc"))
        if _is_mapping(ui_evidence)
        else None
    )
    if github_read_at is not None and checked_at is not None:
        ui_after_readback = checked_at - github_read_at
        if (
            ui_after_readback < -MAX_FUTURE_SKEW
            or ui_after_readback > MAX_UI_AFTER_GITHUB
        ):
            errors.append(
                "snapshot.github.ui_evidence.attestation.document.checked_at_utc: readback_time_mismatch"
            )
    expected_github_digest = canonical_github_readback_sha256(github)
    if (
        expected_github_digest is None
        or ui_evidence.get("github_readback_sha256") != expected_github_digest
    ):
        errors.append(
            "snapshot.github.ui_evidence.attestation.document.github_readback_sha256: readback_digest_mismatch"
        )
    expected_snapshot_digest = canonical_snapshot_readback_sha256(snapshot)
    if (
        expected_snapshot_digest is None
        or ui_evidence.get("snapshot_readback_sha256") != expected_snapshot_digest
    ):
        errors.append(
            "snapshot.github.ui_evidence.attestation.document.snapshot_readback_sha256: snapshot_digest_mismatch"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check-template",
        action="store_true",
        help="validate the local CloudFormation/readback contract without network calls",
    )
    mode.add_argument(
        "--evidence",
        type=Path,
        help="validate a sanitized JSON snapshot; artifact paths resolve under its parent directory, which must be outside the repository",
    )
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    args = parser.parse_args(argv)
    if args.check_template:
        try:
            template = _load_template(args.template)
            errors = _template_static_errors(template)
        except (OSError, yaml.YAMLError, ValueError):
            errors = ["template: unreadable_or_invalid"]
    else:
        try:
            evidence_path = args.evidence.absolute()
            snapshot_mode = evidence_path.lstat()
            if stat.S_ISLNK(snapshot_mode.st_mode) or not stat.S_ISREG(
                snapshot_mode.st_mode
            ):
                print(
                    "readback validation failed: evidence: snapshot_not_regular_file",
                    file=sys.stderr,
                )
                return 2
            if snapshot_mode.st_mode & 0o077 or snapshot_mode.st_nlink != 1:
                print(
                    "readback validation failed: evidence: snapshot_permissions_open",
                    file=sys.stderr,
                )
                return 2
            snapshot = _loads_json_strict(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            print(
                "readback validation failed: evidence: unreadable_or_invalid_json",
                file=sys.stderr,
            )
            return 2
        errors = validate_snapshot(
            snapshot,
            args.template,
            evidence_path.parent,
            expected_describe_regions_response_sha256=(
                PINNED_DESCRIBE_REGIONS_RESPONSE_SHA256
            ),
        )
    if errors:
        print("readback validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("ASAAS producer release readback contract valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
