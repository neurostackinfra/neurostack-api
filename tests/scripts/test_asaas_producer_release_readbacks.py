from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import struct
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scripts import validate_asaas_producer_release_readbacks as validator

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = ROOT / "infra/production/asaas-producer-release.yaml"
STACK_NAME = "asaas-producer-release"
TASK_ROLE_ARN = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-task"
BUILD_ROLE_ARN = f"arn:aws:iam::388460142431:role/{STACK_NAME}-BuildRole-A1B2C3D4"
DEPLOY_ROLE_ARN = f"arn:aws:iam::388460142431:role/{STACK_NAME}-DeployRole-E5F6A7B8"
ECR_NAME = "asaas-producer"
ECR_ARN = f"arn:aws:ecr:us-east-1:388460142431:repository/{ECR_NAME}"
ECR_URI = f"388460142431.dkr.ecr.us-east-1.amazonaws.com/{ECR_NAME}"
BOOTSTRAP_LEDGER_BUCKET = "asaas-producer-release-bootstrap-ledger"
KEY_ARNS = {
    "BuildAttestationSigningKey": "arn:aws:kms:us-east-1:388460142431:key/11111111-1111-4111-8111-111111111111",
    "ProducerGrantSigningKey": "arn:aws:kms:us-east-1:388460142431:key/22222222-2222-4222-8222-222222222222",
    "ProducerReceiptSigningKey": "arn:aws:kms:us-east-1:388460142431:key/33333333-3333-4333-8333-333333333333",
}
EXISTING_DATA_KEY_ARN = (
    "arn:aws:kms:us-east-1:388460142431:key/44444444-4444-4444-8444-444444444444"
)
OTHER_REGION_KEY_ARN = (
    "arn:aws:kms:us-east-2:388460142431:key/55555555-5555-4555-8555-555555555555"
)


def _public_key_pin() -> dict[str, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return {
        "public_key_der_base64": base64.b64encode(der).decode("ascii"),
        "public_key_pem": pem,
        "sha256_fingerprint": f"sha256:{hashlib.sha256(der).hexdigest()}",
    }


def _page(
    items: list[dict[str, Any]],
    request_marker_sha256: str | None = None,
    next_marker_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "request_marker_sha256": request_marker_sha256,
        "next_marker_sha256": next_marker_sha256,
        "truncated": next_marker_sha256 is not None,
        "item_count": len(items),
        "items": items,
    }


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _minimal_png() -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    image_data = zlib.compress(b"\x00\x00\x00\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", image_data)
        + _png_chunk(b"IEND", b"")
    )


def _png_with_scanline(
    scanline: bytes,
    *,
    bit_depth: int = 8,
    color_type: int = 6,
    interlace: int = 0,
    before_idat: tuple[tuple[bytes, bytes], ...] = (),
    after_idat: tuple[tuple[bytes, bytes], ...] = (),
) -> bytes:
    header = struct.pack(">IIBBBBB", 1, 1, bit_depth, color_type, 0, 0, interlace)
    chunks = [_png_chunk(b"IHDR", header)]
    chunks.extend(_png_chunk(kind, data) for kind, data in before_idat)
    chunks.append(_png_chunk(b"IDAT", zlib.compress(scanline)))
    chunks.extend(_png_chunk(kind, data) for kind, data in after_idat)
    chunks.append(_png_chunk(b"IEND", b""))
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def _install_ruleset_screenshot(
    snapshot: dict[str, Any], evidence_root: Path, content: bytes
) -> None:
    screenshot = evidence_root / "ui" / "ruleset.png"
    screenshot.write_bytes(content)
    screenshot.chmod(0o600)
    attestation = _read_attestation(evidence_root)
    attestation["artifacts"]["ruleset"]["sha256"] = hashlib.sha256(content).hexdigest()
    _write_attestation(snapshot, evidence_root, attestation)


def synthetic_snapshot(evidence_root: Path) -> dict[str, Any]:
    """Build a secret-free positive fixture that mirrors normalized service reads."""
    evidence_root.chmod(0o700)
    template = validator._load_template(TEMPLATE_PATH)
    arns = {
        "BuildRole": BUILD_ROLE_ARN,
        "DeployRole": DEPLOY_ROLE_ARN,
        "ExistingTaskRole": TASK_ROLE_ARN,
        **KEY_ARNS,
        "ProducerImageRepository": ECR_ARN,
        "BootstrapSlotLedgerBucket": f"arn:aws:s3:::{BOOTSTRAP_LEDGER_BUCKET}",
    }

    def policy_document(logical_id: str) -> dict[str, Any]:
        return validator._resolve_template_value(
            template["Resources"][logical_id]["Properties"]["PolicyDocument"],
            arns,
            ECR_URI,
            STACK_NAME,
        )

    key_records: dict[str, Any] = {}
    purpose_keys = [
        {
            "logical_id": "BuildAttestationSigningKey",
            "signer": BUILD_ROLE_ARN,
        },
        {"logical_id": "ProducerGrantSigningKey", "signer": TASK_ROLE_ARN},
        {"logical_id": "ProducerReceiptSigningKey", "signer": TASK_ROLE_ARN},
    ]
    for item in purpose_keys:
        logical_id = item["logical_id"]
        arn = KEY_ARNS[logical_id]
        pin = _public_key_pin()
        key_records[logical_id] = {
            "arn": arn,
            "key_state": "Enabled",
            "key_spec": "ECC_NIST_P256",
            "key_usage": "SIGN_VERIFY",
            "signing_algorithms": ["ECDSA_SHA_256"],
            "key_policy": validator._resolve_template_value(
                template["Resources"][logical_id]["Properties"]["KeyPolicy"],
                arns,
                ECR_URI,
                STACK_NAME,
            ),
            "public_key": {"key_id": arn, **pin},
        }

    signing_policy_name = f"{STACK_NAME}-task-signing"
    task_signing_policy = policy_document("ExistingTaskRoleSigningPermissions")
    old_task_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:GenerateDataKey"],
                "Resource": EXISTING_DATA_KEY_ARN,
            }
        ],
    }
    stack_outputs = {
        output_name: validator._resolve_template_value(
            output["Value"], arns, ECR_URI, STACK_NAME, BOOTSTRAP_LEDGER_BUCKET
        )
        for output_name, output in template["Outputs"].items()
    }
    bootstrap_bucket_policy = validator._resolve_template_value(
        template["Resources"]["BootstrapSlotLedgerBucketPolicy"]["Properties"][
            "PolicyDocument"
        ],
        arns,
        ECR_URI,
        STACK_NAME,
        BOOTSTRAP_LEDGER_BUCKET,
    )
    inventory = [
        {
            "arn": KEY_ARNS[logical_id],
            "key_policy": key_records[logical_id]["key_policy"],
            "grants": [],
        }
        for logical_id in validator.KEY_IDS
    ]
    inventory.append(
        {
            "arn": OTHER_REGION_KEY_ARN,
            "key_policy": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": "arn:aws:iam::388460142431:root"},
                        "Action": "kms:*",
                        "Resource": "*",
                    }
                ],
            },
            "grants": [
                {
                    "grantee_principal": "arn:aws:iam::388460142431:role/legacy-encryption-role",
                    "operations": ["Decrypt"],
                },
                {
                    "grantee_principal": "arn:aws:iam::388460142431:role/archive-encryption-role",
                    "operations": ["Encrypt", "DeriveSharedSecret"],
                },
            ],
        }
    )

    now = datetime.now(timezone.utc).replace(microsecond=0)
    collected = now.isoformat().replace("+00:00", "Z")
    checked = (now - timedelta(minutes=3)).isoformat().replace("+00:00", "Z")
    enabled_region_rows = [
        {"region_name": "us-east-1", "opt_in_status": "opt-in-not-required"},
        {"region_name": "us-east-2", "opt_in_status": "opted-in"},
        {"region_name": "ap-south-1", "opt_in_status": "not-opted-in"},
    ]
    raw_regions = [
        {
            "RegionName": row["region_name"],
            "OptInStatus": row["opt_in_status"],
            "Endpoint": f"ec2.{row['region_name']}.amazonaws.com",
            "Geography": [{"Name": "Synthetic"}],
        }
        for row in enabled_region_rows
    ]
    raw_response = {"Regions": raw_regions}
    region_document = {
        "schema_version": "neurostack.aws_ec2_describe_regions_readback.v1",
        "source": "aws_mcp__run_script.call_boto3",
        "service_name": "ec2",
        "operation_name": "DescribeRegions",
        "region_name": "us-east-1",
        "request_params": {"AllRegions": True},
        "api_calls": [
            {
                "service": "ec2",
                "operation": "DescribeRegions",
                "status": "success",
                "n_items": {"Regions": len(raw_regions)},
            }
        ],
        "read_at_utc": collected,
        "response": raw_response,
        "response_sha256": hashlib.sha256(
            json.dumps(raw_response, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    region_bytes = json.dumps(region_document, sort_keys=True).encode("utf-8")
    region_artifact_path = evidence_root / "aws" / "describe-regions.json"
    region_artifact_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    region_artifact_path.parent.chmod(0o700)
    region_artifact_path.write_bytes(region_bytes)
    region_artifact_path.chmod(0o600)
    region_artifact = {
        "path": "aws/describe-regions.json",
        "sha256": hashlib.sha256(region_bytes).hexdigest(),
    }
    east_one_key_arns = [KEY_ARNS[logical_id] for logical_id in validator.KEY_IDS]
    east_two_key_arns = [OTHER_REGION_KEY_ARN]
    grants_by_arn = {record["arn"]: record["grants"] for record in inventory}
    regional_scans = {
        "us-east-1": {
            "list_keys_pages": [
                _page(
                    [{"arn": arn} for arn in east_one_key_arns[:2]],
                    next_marker_sha256="1" * 64,
                ),
                _page(
                    [{"arn": arn} for arn in east_one_key_arns[2:]],
                    request_marker_sha256="1" * 64,
                ),
            ],
            "key_grant_scans": {
                arn: {"list_grants_pages": [_page(grants_by_arn[arn])]}
                for arn in east_one_key_arns
            },
        },
        "us-east-2": {
            "list_keys_pages": [_page([{"arn": arn} for arn in east_two_key_arns])],
            "key_grant_scans": {
                OTHER_REGION_KEY_ARN: {
                    "list_grants_pages": [
                        _page(
                            grants_by_arn[OTHER_REGION_KEY_ARN][:1],
                            next_marker_sha256="2" * 64,
                        ),
                        _page(
                            grants_by_arn[OTHER_REGION_KEY_ARN][1:],
                            request_marker_sha256="2" * 64,
                        ),
                    ]
                }
            },
        },
    }
    github = {
        "read_at_utc": collected,
        "repository": {
            "endpoint": "/repos/neurostackinfra/neurostack-api",
            "http_status": 200,
            "full_name": "neurostackinfra/neurostack-api",
            "id": 1152335815,
            "owner_id": 340698182,
            "default_branch": "main",
        },
        "attestor_identity": {
            "endpoint": "/user",
            "http_status": 200,
            "login": "independent-admin",
            "id": 71234,
        },
        "attestor_repository_permission": {
            "endpoint": "/repos/neurostackinfra/neurostack-api/collaborators/independent-admin/permission",
            "http_status": 200,
            "login": "independent-admin",
            "id": 71234,
            "permission": "admin",
        },
        "oidc_subject_customization": {
            "endpoint": "/repos/neurostackinfra/neurostack-api/actions/oidc/customization/sub",
            "http_status": 200,
            "use_default": False,
            "use_immutable_subject": True,
            "sub_claim_prefix": "repo:neurostackinfra@340698182/neurostack-api@1152335815",
            "include_claim_keys": ["repo", "context", "ref", "workflow_ref"],
        },
        "rulesets": {
            "endpoint": "/repos/neurostackinfra/neurostack-api/rulesets",
            "http_status": 200,
            "pages": [
                {
                    "request_page": 1,
                    "next_page": None,
                    "item_count": 1,
                    "item_ids": [1234],
                }
            ],
            "items": [
                {
                    "id": 1234,
                    "name": "asaas-main-release",
                    "target": "branch",
                    "enforcement": "active",
                    "conditions": {
                        "ref_name": {
                            "include": ["refs/heads/main"],
                            "exclude": [],
                        }
                    },
                    "bypass_actors": [],
                    "rules": [
                        {
                            "type": "pull_request",
                            "parameters": {
                                "required_approving_review_count": 1,
                                "dismiss_stale_reviews_on_push": True,
                                "require_last_push_approval": True,
                            },
                        },
                        {
                            "type": "required_status_checks",
                            "parameters": {
                                "required_status_checks": [
                                    {
                                        "context": "ASAAS producer build / attest",
                                        "integration_id": 15368,
                                    }
                                ],
                                "strict_required_status_checks_policy": True,
                            },
                        },
                        {"type": "deletion"},
                        {"type": "non_fast_forward"},
                    ],
                }
            ],
        },
        "environments": {
            environment_name: {
                "environment_endpoint": f"/repos/neurostackinfra/neurostack-api/environments/{environment_name}",
                "environment_http_status": 200,
                "environment": {
                    "name": environment_name,
                    "can_admins_bypass": False,
                    "protection_rules": [
                        {
                            "type": "required_reviewers",
                            "reviewers": [
                                {
                                    "type": "Team",
                                    "id": 98273,
                                    "name": "neurostack-admins",
                                }
                            ],
                            "prevent_self_review": True,
                        }
                    ],
                    "deployment_branch_policy": {
                        "protected_branches": False,
                        "custom_branch_policies": True,
                    },
                },
                "branch_policies_endpoint": f"/repos/neurostackinfra/neurostack-api/environments/{environment_name}/deployment-branch-policies",
                "branch_policies_http_status": 200,
                "branch_policies": {
                    "total_count": 1,
                    "branch_policies": [{"name": "main"}],
                },
            }
            for environment_name in validator.ENVIRONMENTS
        },
    }
    screenshot_artifacts: dict[str, dict[str, str]] = {}
    for artifact_name in ("ruleset", *validator.ENVIRONMENTS):
        relative_path = f"ui/{artifact_name}.png"
        artifact_path = evidence_root / relative_path
        artifact_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact_path.parent.chmod(0o700)
        screenshot_bytes = _minimal_png()
        artifact_path.write_bytes(screenshot_bytes)
        artifact_path.chmod(0o600)
        screenshot_artifacts[artifact_name] = {
            "path": relative_path,
            "sha256": hashlib.sha256(screenshot_bytes).hexdigest(),
        }
    attestation = {
        "schema_version": "neurostack.asaas_producer_release_ui_attestation.v1",
        "reviewer": {
            "login": "independent-admin",
            "id": 71234,
            "repository_role": "admin",
            "is_independent_repository_admin": True,
        },
        "checked_at_utc": checked,
        "readback_id": str(uuid.uuid4()),
        "ref": "refs/heads/main",
        "environments": list(validator.ENVIRONMENTS),
        "github_readback_sha256": validator.canonical_github_readback_sha256(github),
        "snapshot_readback_sha256": "0" * 64,
        "artifacts": screenshot_artifacts,
        "ruleset_bypass_empty": True,
        "environment_admin_bypass_disabled": {
            environment_name: True for environment_name in validator.ENVIRONMENTS
        },
        "environment_branch_policy_names": {
            environment_name: ["main"] for environment_name in validator.ENVIRONMENTS
        },
        "environment_branch_only_no_tags": {
            environment_name: True for environment_name in validator.ENVIRONMENTS
        },
    }
    attestation_bytes = json.dumps(attestation, sort_keys=True).encode("utf-8")
    attestation_path = evidence_root / "ui" / "attestation.json"
    attestation_path.write_bytes(attestation_bytes)
    attestation_path.chmod(0o600)
    github["ui_evidence"] = {
        "attestation": {
            "path": "ui/attestation.json",
            "sha256": hashlib.sha256(attestation_bytes).hexdigest(),
        }
    }
    snapshot = {
        "schema_version": validator.READBACK_SCHEMA_VERSION,
        "collected_at_utc": collected,
        "aws": {
            "read_at_utc": collected,
            "account_id": "388460142431",
            "region": "us-east-1",
            "stack": {
                "name": STACK_NAME,
                "status": "CREATE_COMPLETE",
                "outputs": stack_outputs,
            },
            "oidc_provider": {
                "arn": "arn:aws:iam::388460142431:oidc-provider/token.actions.githubusercontent.com",
                "issuer": "https://token.actions.githubusercontent.com",
                "url": "token.actions.githubusercontent.com",
                "client_ids": ["sts.amazonaws.com"],
            },
            "roles": {
                "BuildRole": {
                    "arn": BUILD_ROLE_ARN,
                    "assume_role_policy": template["Resources"]["BuildRole"][
                        "Properties"
                    ]["AssumeRolePolicyDocument"],
                    "attached_managed_policy_pages": [_page([])],
                    "inline_policy_pages": [
                        _page([{"policy_name": f"{STACK_NAME}-build"}])
                    ],
                    "attached_managed_policies": [],
                    "inline_policies": {
                        f"{STACK_NAME}-build": policy_document("BuildRolePermissions")
                    },
                },
                "DeployRole": {
                    "arn": DEPLOY_ROLE_ARN,
                    "assume_role_policy": template["Resources"]["DeployRole"][
                        "Properties"
                    ]["AssumeRolePolicyDocument"],
                    "attached_managed_policy_pages": [_page([])],
                    "inline_policy_pages": [
                        _page([{"policy_name": f"{STACK_NAME}-deploy"}])
                    ],
                    "attached_managed_policies": [],
                    "inline_policies": {
                        f"{STACK_NAME}-deploy": policy_document("DeployRolePermissions")
                    },
                },
                "ExistingTaskRole": {
                    "arn": TASK_ROLE_ARN,
                    "assume_role_policy": validator._task_role_trust(),
                    "attached_managed_policy_pages": [_page([])],
                    "inline_policy_pages": [
                        _page(
                            [
                                {"policy_name": "neurostack-staging-task-runtime"},
                                {"policy_name": signing_policy_name},
                            ]
                        )
                    ],
                    "attached_managed_policies": [],
                    "inline_policies": {
                        "neurostack-staging-task-runtime": old_task_policy,
                        signing_policy_name: task_signing_policy,
                    },
                },
            },
            "keys": key_records,
            "kms_authorization_inventory": {
                "describe_regions_artifact": region_artifact,
                "regional_scans": regional_scans,
                "inspected_key_count": len(inventory),
                "keys": inventory,
            },
            "ecr": {
                "repository_name": ECR_NAME,
                "repository_arn": ECR_ARN,
                "repository_uri": ECR_URI,
                "image_tag_mutability": "IMMUTABLE",
                "encryption_type": "AES256",
                "scan_on_push": True,
            },
            "bootstrap_slot_ledger": {
                "read_at_utc": collected,
                "bucket_name": BOOTSTRAP_LEDGER_BUCKET,
                "versioning_status": "Enabled",
                "object_lock_configuration": {
                    "object_lock_enabled": "Enabled",
                    "default_retention_mode": "COMPLIANCE",
                    "default_retention_years": 100,
                },
                "bucket_encryption": {"sse_algorithm": "AES256"},
                "public_access_block": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                },
                "lifecycle_rules": [],
                "bucket_policy": bootstrap_bucket_policy,
                "build_slot_versions_pages": [_page([])],
                "deployment_slot_versions_pages": [_page([])],
            },
        },
        "github": github,
    }
    attestation["snapshot_readback_sha256"] = (
        validator.canonical_snapshot_readback_sha256(snapshot)
    )
    attestation_bytes = json.dumps(attestation, sort_keys=True).encode("utf-8")
    attestation_path.write_bytes(attestation_bytes)
    attestation_path.chmod(0o600)
    snapshot["github"]["ui_evidence"]["attestation"]["sha256"] = hashlib.sha256(
        attestation_bytes
    ).hexdigest()
    return snapshot


def _mutate(snapshot: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    target: Any = snapshot
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


def _release_rule(snapshot: dict[str, Any], rule_type: str) -> dict[str, Any]:
    rules = snapshot["github"]["rulesets"]["items"][0]["rules"]
    return next(rule for rule in rules if rule["type"] == rule_type)


def _task_policy(snapshot: dict[str, Any]) -> dict[str, Any]:
    policies = snapshot["aws"]["roles"]["ExistingTaskRole"]["inline_policies"]
    old_policy_name = next(
        name for name in policies if not name.endswith("-task-signing")
    )
    return policies[old_policy_name]


def _key_inventory_record(snapshot: dict[str, Any], key_arn: str) -> dict[str, Any]:
    return next(
        record
        for record in snapshot["aws"]["kms_authorization_inventory"]["keys"]
        if record["arn"] == key_arn
    )


def _read_attestation(evidence_root: Path) -> dict[str, Any]:
    return json.loads(
        (evidence_root / "ui" / "attestation.json").read_text(encoding="utf-8")
    )


def _write_attestation(
    snapshot: dict[str, Any], evidence_root: Path, attestation: dict[str, Any]
) -> None:
    content = json.dumps(attestation, sort_keys=True).encode("utf-8")
    (evidence_root / "ui" / "attestation.json").write_bytes(content)
    snapshot["github"]["ui_evidence"]["attestation"]["sha256"] = hashlib.sha256(
        content
    ).hexdigest()
    (evidence_root / "ui" / "attestation.json").chmod(0o600)


def _refresh_attestation_digests(
    snapshot: dict[str, Any],
    evidence_root: Path,
    *,
    include_github_digest: bool = False,
) -> None:
    attestation = _read_attestation(evidence_root)
    if include_github_digest:
        attestation["github_readback_sha256"] = (
            validator.canonical_github_readback_sha256(snapshot["github"])
        )
    attestation["snapshot_readback_sha256"] = (
        validator.canonical_snapshot_readback_sha256(snapshot)
    )
    _write_attestation(snapshot, evidence_root, attestation)


def _rewrite_region_document(
    snapshot: dict[str, Any],
    evidence_root: Path,
    mutate: Callable[[dict[str, Any]], Any],
) -> None:
    path = evidence_root / "aws" / "describe-regions.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    content = json.dumps(document, sort_keys=True).encode("utf-8")
    path.write_bytes(content)
    path.chmod(0o600)
    snapshot["aws"]["kms_authorization_inventory"]["describe_regions_artifact"][
        "sha256"
    ] = hashlib.sha256(content).hexdigest()
    _refresh_attestation_digests(snapshot, evidence_root)


def test_synthetic_authoritative_snapshot_passes_without_network_calls(
    tmp_path: Path,
) -> None:
    assert (
        validator.validate_snapshot(
            synthetic_snapshot(tmp_path), evidence_root=tmp_path
        )
        == []
    )


def test_inventory_key_policy_accepts_optional_aws_id(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["aws"]["kms_authorization_inventory"]["keys"][3]["key_policy"][
        "Id"
    ] = "auto-secretsmanager-2"
    _refresh_attestation_digests(snapshot, tmp_path)
    assert validator.validate_snapshot(snapshot, evidence_root=tmp_path) == []


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        ("Id", "", "key_policy.Id: invalid_policy_id"),
        ("Unexpected", "extra", "key_policy: unexpected_field"),
    ],
    ids=["empty-id", "extra-top-level-field"],
)
def test_inventory_key_policy_rejects_invalid_id_and_extra_fields(
    field: str, value: str, expected_error: str, tmp_path: Path
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["aws"]["kms_authorization_inventory"]["keys"][3]["key_policy"][
        field
    ] = value
    _refresh_attestation_digests(snapshot, tmp_path)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(expected_error in error for error in errors)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: _mutate(snapshot, ("aws", "account_id"), "000000000000"),
        lambda snapshot: _mutate(snapshot, ("aws", "region"), "us-east-2"),
        lambda snapshot: _mutate(
            snapshot, ("aws", "oidc_provider", "issuer"), "https://example.invalid"
        ),
        lambda snapshot: _mutate(
            snapshot, ("aws", "oidc_provider", "client_ids"), ["wrong-audience"]
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "roles",
                "BuildRole",
                "assume_role_policy",
                "Statement",
                0,
                "Condition",
                "StringEquals",
                "token.actions.githubusercontent.com:sub",
            ),
            "repo:neurostackinfra/*",
        ),
        lambda snapshot: _mutate(
            snapshot, ("aws", "roles", "DeployRole", "arn"), BUILD_ROLE_ARN
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "roles",
                "ExistingTaskRole",
                "assume_role_policy",
                "Statement",
                0,
                "Principal",
            ),
            "*",
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "roles",
                "ExistingTaskRole",
                "assume_role_policy",
                "Statement",
                0,
                "Sid",
            ),
            "DifferentSid",
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "roles",
                "ExistingTaskRole",
                "assume_role_policy",
                "Statement",
                0,
                "Condition",
                "StringEquals",
                "aws:SourceAccount",
            ),
            "000000000000",
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "roles", "BuildRole", "inline_policy_pages"),
            [],
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "roles",
                "BuildRole",
                "inline_policies",
                f"{STACK_NAME}-build",
                "Statement",
                1,
                "Action",
            ),
            ["ecs:UpdateService"],
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "roles",
                "DeployRole",
                "inline_policies",
                f"{STACK_NAME}-deploy",
                "Statement",
                2,
                "Resource",
            ),
            "arn:aws:iam::388460142431:role/*",
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "roles",
                "DeployRole",
                "inline_policies",
                f"{STACK_NAME}-deploy",
                "Statement",
                2,
                "Condition",
            ),
            {},
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "roles", "ExistingTaskRole", "attached_managed_policies"),
            ["unexpected-managed-policy"],
        ),
        lambda snapshot: _task_policy(snapshot)["Statement"][0].update(
            Action=["kms:GenerateDataKey", "kms:Decrypt", "kms:Sign"]
        ),
        lambda snapshot: _task_policy(snapshot)["Statement"][0].update(Action="kms:*"),
        lambda snapshot: snapshot["aws"]["roles"]["ExistingTaskRole"][
            "inline_policies"
        ].update(
            {
                "extra-signing": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {"Effect": "Allow", "Action": "kms:Sign", "Resource": "*"}
                    ],
                }
            }
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "keys", "ProducerGrantSigningKey", "arn"),
            KEY_ARNS["ProducerReceiptSigningKey"],
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "keys", "BuildAttestationSigningKey", "key_state"),
            "Disabled",
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "keys", "ProducerReceiptSigningKey", "key_spec"),
            "ECC_NIST_P384",
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "keys", "ProducerGrantSigningKey", "key_usage"),
            "ENCRYPT_DECRYPT",
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "keys", "BuildAttestationSigningKey", "signing_algorithms"),
            ["ECDSA_SHA_512"],
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "keys", "ProducerGrantSigningKey", "public_key", "public_key_pem"),
            "not-the-matching-pem",
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "keys",
                "ProducerReceiptSigningKey",
                "public_key",
                "sha256_fingerprint",
            ),
            "sha256:bad",
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "keys",
                "ProducerReceiptSigningKey",
                "key_policy",
                "Statement",
                1,
                "Principal",
                "AWS",
            ),
            "arn:aws:iam::388460142431:role/*",
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "ecr", "repository_uri"),
            "000000000000.dkr.ecr.us-east-1.amazonaws.com/other",
        ),
        lambda snapshot: _mutate(
            snapshot, ("aws", "ecr", "image_tag_mutability"), "MUTABLE"
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "stack", "outputs", "ProducerGrantSigningKeyArn"),
            KEY_ARNS["ProducerReceiptSigningKey"],
        ),
        lambda snapshot: _mutate(
            snapshot, ("github", "oidc_subject_customization", "use_default"), True
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("github", "oidc_subject_customization", "use_immutable_subject"),
            False,
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("github", "oidc_subject_customization", "include_claim_keys"),
            ["repo", "ref", "context", "workflow_ref"],
        ),
        lambda snapshot: _mutate(
            snapshot, ("github", "rulesets", "pages", 0, "next_page"), 2
        ),
        lambda snapshot: _mutate(
            snapshot, ("github", "rulesets", "items", 0, "enforcement"), "evaluate"
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("github", "rulesets", "items", 0, "conditions", "ref_name", "include"),
            ["refs/heads/*"],
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("github", "rulesets", "items", 0, "bypass_actors"),
            [{"actor_id": 1}],
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "github",
                "rulesets",
                "items",
                0,
                "rules",
                0,
                "parameters",
                "dismiss_stale_reviews_on_push",
            ),
            False,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "github",
                "rulesets",
                "items",
                0,
                "rules",
                0,
                "parameters",
                "require_last_push_approval",
            ),
            False,
        ),
        lambda snapshot: _release_rule(snapshot, "required_status_checks")[
            "parameters"
        ].update(required_status_checks=[]),
        lambda snapshot: snapshot["github"]["rulesets"]["items"][0]["rules"].remove(
            _release_rule(snapshot, "non_fast_forward")
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "github",
                "environments",
                "asaas-build",
                "environment",
                "protection_rules",
                0,
                "prevent_self_review",
            ),
            False,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "github",
                "environments",
                "asaas-build",
                "environment",
                "can_admins_bypass",
            ),
            True,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "github",
                "environments",
                "asaas-production-deployment",
                "environment",
                "deployment_branch_policy",
                "protected_branches",
            ),
            True,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "github",
                "environments",
                "asaas-build",
                "branch_policies",
                "branch_policies",
                0,
                "name",
            ),
            "release/*",
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "github",
                "environments",
                "asaas-production-deployment",
                "branch_policies",
                "branch_policies",
                0,
            ),
            {"name": "main", "type": "tag"},
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("github", "ui_evidence", "attestation", "path"),
            "../attestation.json",
        ),
        lambda snapshot: _mutate(
            snapshot, ("github", "ui_evidence", "attestation", "sha256"), "a" * 64
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "regional_scans",
                "us-east-1",
                "list_keys_pages",
                1,
                "request_marker_sha256",
            ),
            "f" * 64,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "regional_scans",
                "us-east-1",
                "list_keys_pages",
                0,
                "item_count",
            ),
            99,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "regional_scans",
                "us-east-1",
                "list_keys_pages",
                1,
                "next_marker_sha256",
            ),
            "3" * 64,
        ),
        lambda snapshot: snapshot["aws"]["kms_authorization_inventory"][
            "regional_scans"
        ]["us-east-1"]["list_keys_pages"].pop(),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "regional_scans",
                "us-east-2",
                "key_grant_scans",
                OTHER_REGION_KEY_ARN,
                "list_grants_pages",
                1,
                "request_marker_sha256",
            ),
            "f" * 64,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "regional_scans",
                "us-east-2",
                "key_grant_scans",
                OTHER_REGION_KEY_ARN,
                "list_grants_pages",
                0,
                "item_count",
            ),
            99,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "regional_scans",
                "us-east-2",
                "key_grant_scans",
                OTHER_REGION_KEY_ARN,
                "list_grants_pages",
                0,
                "next_marker_sha256",
            ),
            "4" * 64,
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "regional_scans",
                "us-east-2",
                "key_grant_scans",
                OTHER_REGION_KEY_ARN,
                "list_grants_pages",
                0,
                "items",
                0,
                "operations",
            ),
            ["sign"],
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "keys",
                3,
                "grants",
                0,
                "operations",
            ),
            ["sign"],
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "keys",
                3,
                "key_policy",
                "Statement",
            ),
            [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "kms:Sign",
                    "Resource": "*",
                }
            ],
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "kms_authorization_inventory", "keys", 3, "grants"),
            [{"grantee_principal": TASK_ROLE_ARN, "operations": ["Sign"]}],
        ),
        lambda snapshot: _mutate(
            snapshot,
            ("aws", "kms_authorization_inventory", "keys", 3, "grants"),
            [
                {
                    "grantee_principal": "arn:aws:sts::388460142431:assumed-role/neurostack-staging-ecs-task/ecs-task-session-0123456789abcdef",
                    "operations": ["Sign"],
                }
            ],
        ),
        lambda snapshot: _mutate(
            snapshot,
            (
                "aws",
                "kms_authorization_inventory",
                "keys",
                3,
                "key_policy",
                "Statement",
            ),
            [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "AWS": "arn:aws:iam::388460142431:role/neurostack-staging-*"
                    },
                    "Action": "kms:Sign",
                    "Resource": "*",
                }
            ],
        ),
        lambda snapshot: snapshot["aws"].update(unexpected_field="must-not-be-ignored"),
    ],
    ids=[
        "wrong-account",
        "wrong-region",
        "wrong-oidc-issuer",
        "wrong-audience",
        "wildcard-build-trust",
        "build-deploy-role-alias",
        "task-role-trust-expanded",
        "task-role-trust-sid-mismatch",
        "task-role-source-account-mismatch",
        "incomplete-role-policy-list",
        "build-can-update-ecs",
        "broad-pass-role",
        "pass-role-condition-missing",
        "task-managed-policy-added",
        "task-role-signs-unapproved-key",
        "task-role-kms-wildcard",
        "extra-task-signing-policy",
        "duplicate-key-arn",
        "disabled-key",
        "wrong-key-spec",
        "wrong-key-usage",
        "wrong-signing-algorithm",
        "public-key-pem-mismatch",
        "public-key-pin-mismatch",
        "wrong-key-policy-principal",
        "wrong-ecr-uri",
        "mutable-ecr-tags",
        "wrong-cloudformation-output",
        "default-oidc-subject",
        "mutable-subject",
        "wrong-ordered-claim-keys",
        "incomplete-ruleset-pagination",
        "inactive-ruleset",
        "wildcard-main-target",
        "ruleset-bypass-present",
        "stale-review-not-dismissed",
        "last-push-not-approved",
        "required-check-missing",
        "force-push-not-blocked",
        "environment-self-review",
        "environment-admin-bypass",
        "all-protected-branches-policy",
        "wrong-environment-branch",
        "tag-policy",
        "ui-attestation-path-escape",
        "ui-attestation-digest-mismatch",
        "list-keys-token-chain-broken",
        "list-keys-count-mismatch",
        "list-keys-terminal-token",
        "list-keys-page-missing",
        "list-grants-token-chain-broken",
        "list-grants-count-mismatch",
        "list-grants-terminal-token",
        "list-grants-case-variant-operation",
        "aggregate-grant-case-variant-operation",
        "other-key-policy-allows-task-sign",
        "other-key-grant-allows-task-sign",
        "assumed-task-role-signing-grant",
        "wildcard-task-role-key-policy-principal",
        "unexpected-top-level-field",
    ],
)
def test_snapshot_mutations_fail_closed(
    mutate: Callable[[dict[str, Any]], Any], tmp_path: Path
) -> None:
    snapshot = copy.deepcopy(synthetic_snapshot(tmp_path))
    mutate(snapshot)
    assert validator.validate_snapshot(snapshot, evidence_root=tmp_path)


def test_snapshot_rejects_missing_and_extra_fields(tmp_path: Path) -> None:
    missing = synthetic_snapshot(tmp_path)
    del missing["github"]["ui_evidence"]
    errors = validator.validate_snapshot(missing, evidence_root=tmp_path)
    assert any(
        "snapshot.github.ui_evidence: missing_field" in error for error in errors
    )

    extra = synthetic_snapshot(tmp_path)
    extra["aws"]["ecr"]["sentinel-secret-do-not-print"] = "never-accepted"
    errors = validator.validate_snapshot(extra, evidence_root=tmp_path)
    assert any("snapshot.aws.ecr: unexpected_field" in error for error in errors)
    assert all("never-accepted" not in error for error in errors)
    assert all("sentinel-secret-do-not-print" not in error for error in errors)


@pytest.mark.parametrize("kind", ["version", "delete_marker"])
def test_bootstrap_slot_ledger_rejects_any_prior_version_or_delete_marker(
    kind: str, tmp_path: Path
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    page = snapshot["aws"]["bootstrap_slot_ledger"]["build_slot_versions_pages"][0]
    page["items"].append(
        {"key": "slots/build-bootstrap.json", "version_id": "v1", "kind": kind}
    )
    page["item_count"] = len(page["items"])
    _refresh_attestation_digests(snapshot, tmp_path)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("build_slot_versions_pages: slot_not_empty" in error for error in errors)


def test_bootstrap_slot_ledger_requires_terminal_version_history_page(
    tmp_path: Path,
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    page = snapshot["aws"]["bootstrap_slot_ledger"]["deployment_slot_versions_pages"][0]
    page["truncated"] = True
    page["next_marker_sha256"] = "a" * 64
    _refresh_attestation_digests(snapshot, tmp_path)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(
        "deployment_slot_versions_pages[0]: page_missing_after_next_marker" in error
        for error in errors
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("versioning_status", "Suspended", "versioning_status: mismatch"),
        (
            "object_lock_configuration",
            {
                "object_lock_enabled": "Enabled",
                "default_retention_mode": "GOVERNANCE",
                "default_retention_years": 100,
            },
            "object_lock_configuration: mismatch",
        ),
        (
            "bucket_encryption",
            {"sse_algorithm": "aws:kms"},
            "bucket_encryption: mismatch",
        ),
        (
            "public_access_block",
            {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": False,
            },
            "public_access_block: mismatch",
        ),
        ("lifecycle_rules", [{"expiration_days": 365}], "lifecycle_rules: mismatch"),
    ],
)
def test_bootstrap_slot_ledger_controls_fail_closed(
    field: str, value: Any, reason: str, tmp_path: Path
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["aws"]["bootstrap_slot_ledger"][field] = value
    _refresh_attestation_digests(snapshot, tmp_path)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(reason in error for error in errors)


def test_role_policy_lists_require_complete_paged_evidence(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["aws"]["roles"]["ExistingTaskRole"]["inline_policy_pages"] = []
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("inline_policy_pages: missing_pages" in error for error in errors)

    snapshot = synthetic_snapshot(tmp_path)
    pages = snapshot["aws"]["roles"]["ExistingTaskRole"]["inline_policy_pages"]
    pages[0]["next_marker_sha256"] = "a" * 64
    pages[0]["truncated"] = True
    pages.append(_page([], request_marker_sha256="b" * 64))
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(
        "request_marker_sha256: token_chain_mismatch" in error for error in errors
    )


def test_ruleset_pages_bind_counts_items_and_terminal_cursor(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["github"]["rulesets"]["pages"] = []
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("rulesets.pages: missing_pages" in error for error in errors)

    snapshot = synthetic_snapshot(tmp_path)
    snapshot["github"]["rulesets"]["pages"][0]["next_page"] = 2
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("next_page: page_missing_after_next_link" in error for error in errors)


@pytest.mark.parametrize(
    ("snapshot_section", "field", "value", "reason"),
    [
        (
            "attestor_identity",
            "login",
            "other-reviewer",
            "attestor_repository_permission.login",
        ),
        (
            "attestor_repository_permission",
            "permission",
            "write",
            "attestor_repository_permission.permission",
        ),
    ],
)
def test_ui_attestor_requires_github_identity_and_permission_readbacks(
    snapshot_section: str, field: str, value: str, reason: str, tmp_path: Path
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["github"][snapshot_section][field] = value
    _refresh_attestation_digests(snapshot, tmp_path, include_github_digest=True)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(reason in error for error in errors)


def test_ui_attestor_claim_must_match_github_current_user(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    attestation = _read_attestation(tmp_path)
    attestation["reviewer"]["login"] = "claimed-admin"
    _write_attestation(snapshot, tmp_path, attestation)
    _refresh_attestation_digests(snapshot, tmp_path, include_github_digest=True)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("identity_readback_mismatch" in error for error in errors)


def test_source_timestamps_reject_stale_and_future_reads(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["aws"]["read_at_utc"] = (
        (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path, now_utc=now)
    assert "snapshot.aws.read_at_utc: stale_evidence" in errors

    snapshot = synthetic_snapshot(tmp_path)
    snapshot["aws"]["read_at_utc"] = (
        (now + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path, now_utc=now)
    assert "snapshot.aws.read_at_utc: future_evidence" in errors

    snapshot = synthetic_snapshot(tmp_path)
    snapshot["github"]["read_at_utc"] = (
        (now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path, now_utc=now)
    assert "snapshot.github.read_at_utc: stale_evidence" in errors

    snapshot = synthetic_snapshot(tmp_path)
    snapshot["github"]["read_at_utc"] = (
        (now + timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path, now_utc=now)
    assert "snapshot.github.read_at_utc: future_evidence" in errors


def test_describe_regions_artifact_timestamp_and_count_are_checked(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot = synthetic_snapshot(tmp_path)
    _rewrite_region_document(
        snapshot,
        tmp_path,
        lambda document: document.update(
            read_at_utc=(now - timedelta(hours=25)).isoformat().replace("+00:00", "Z")
        ),
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path, now_utc=now)
    assert any(
        "describe_regions_artifact.document.read_at_utc: stale_evidence" in error
        for error in errors
    )

    snapshot = synthetic_snapshot(tmp_path)
    _rewrite_region_document(
        snapshot,
        tmp_path,
        lambda document: document["api_calls"][0]["n_items"].update(Regions=99),
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path, now_utc=now)
    assert any(
        "describe_regions_artifact.document.api_calls[0].n_items: mismatch" in error
        for error in errors
    )


@pytest.mark.parametrize("parameters", [{}, {"AllRegions": False}])
def test_describe_regions_requires_explicit_all_regions_request(
    tmp_path: Path, parameters: dict[str, Any]
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    _rewrite_region_document(
        snapshot,
        tmp_path,
        lambda document: document.update(request_params=parameters),
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert (
        "snapshot.aws.kms_authorization_inventory.describe_regions_artifact.document.request_params: mismatch"
        in errors
    )


def test_describe_regions_raw_response_digest_must_match(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    _rewrite_region_document(
        snapshot,
        tmp_path,
        lambda document: document.update(response_sha256="0" * 64),
    )
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert (
        "snapshot.aws.kms_authorization_inventory.describe_regions_artifact.document.response_sha256: mismatch"
        in errors
    )


def test_describe_regions_omission_breaks_exact_scan_coverage(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    del snapshot["aws"]["kms_authorization_inventory"]["regional_scans"]["us-east-2"]
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert (
        "snapshot.aws.kms_authorization_inventory.regional_scans: enabled_region_coverage_mismatch"
        in errors
    )


def test_paired_describe_regions_omission_fails_against_independent_response_pin(
    tmp_path: Path,
) -> None:
    """Recomputed local hashes cannot override the independently pinned source."""
    snapshot = synthetic_snapshot(tmp_path)
    source_path = (
        tmp_path
        / snapshot["aws"]["kms_authorization_inventory"]["describe_regions_artifact"][
            "path"
        ]
    )
    pinned_response_sha256 = json.loads(source_path.read_text())["response_sha256"]

    def remove_region(document: dict[str, Any]) -> None:
        document["response"]["Regions"].pop(1)
        document["api_calls"][0]["n_items"]["Regions"] -= 1
        document["response_sha256"] = hashlib.sha256(
            json.dumps(
                document["response"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    _rewrite_region_document(snapshot, tmp_path, remove_region)
    snapshot["aws"]["kms_authorization_inventory"]["regional_scans"].pop("us-east-2")
    snapshot["aws"]["kms_authorization_inventory"]["keys"] = [
        key
        for key in snapshot["aws"]["kms_authorization_inventory"]["keys"]
        if ":us-east-2:" not in key["arn"]
    ]
    snapshot["aws"]["kms_authorization_inventory"]["inspected_key_count"] -= 1
    _refresh_attestation_digests(snapshot, tmp_path)

    errors = validator.validate_snapshot(
        snapshot,
        evidence_root=tmp_path,
        expected_describe_regions_response_sha256=pinned_response_sha256,
    )
    assert (
        "snapshot.aws.kms_authorization_inventory.describe_regions_artifact.document.response_sha256_pinned: mismatch"
        in errors
    )

    snapshot = synthetic_snapshot(tmp_path)
    _rewrite_region_document(snapshot, tmp_path, remove_region)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert (
        "snapshot.aws.kms_authorization_inventory.regional_scans: enabled_region_coverage_mismatch"
        in errors
    )


def test_evidence_cli_rejects_rehashed_paired_region_omission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        validator.PINNED_DESCRIBE_REGIONS_RESPONSE_SHA256
        == "f4e52f7420e339d831af2555ef035e75316cc01351296330ed68da7f119a6e00"
    )
    snapshot = synthetic_snapshot(tmp_path)
    source_path = (
        tmp_path
        / snapshot["aws"]["kms_authorization_inventory"]["describe_regions_artifact"][
            "path"
        ]
    )
    fixture_pin = json.loads(source_path.read_text())["response_sha256"]
    monkeypatch.setattr(
        validator, "PINNED_DESCRIBE_REGIONS_RESPONSE_SHA256", fixture_pin
    )

    def remove_region(document: dict[str, Any]) -> None:
        document["response"]["Regions"].pop(1)
        document["api_calls"][0]["n_items"]["Regions"] -= 1
        document["response_sha256"] = hashlib.sha256(
            json.dumps(
                document["response"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    _rewrite_region_document(snapshot, tmp_path, remove_region)
    inventory = snapshot["aws"]["kms_authorization_inventory"]
    inventory["regional_scans"].pop("us-east-2")
    inventory["keys"] = [
        key for key in inventory["keys"] if ":us-east-2:" not in key["arn"]
    ]
    inventory["inspected_key_count"] -= 1
    _refresh_attestation_digests(snapshot, tmp_path)
    evidence_path = tmp_path / "snapshot.json"
    evidence_path.write_text(json.dumps(snapshot, sort_keys=True))
    evidence_path.chmod(0o600)

    assert validator.main(["--evidence", str(evidence_path)]) == 1
    assert "response_sha256_pinned: mismatch" in capsys.readouterr().err


def test_attestation_time_is_compared_with_github_readback(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    attestation = _read_attestation(tmp_path)
    github_read_at = datetime.fromisoformat(
        snapshot["github"]["read_at_utc"].replace("Z", "+00:00")
    )
    attestation["checked_at_utc"] = (
        (github_read_at - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    )
    _write_attestation(snapshot, tmp_path, attestation)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("checked_at_utc: readback_time_mismatch" in error for error in errors)


def test_ui_artifact_missing_changed_and_invalid_png_fail(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    screenshot = tmp_path / "ui" / "ruleset.png"
    screenshot.write_bytes(b"changed-content")
    screenshot.chmod(0o600)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(
        "artifacts.ruleset.sha256: artifact_digest_mismatch" in error
        for error in errors
    )

    snapshot = synthetic_snapshot(tmp_path)
    screenshot = tmp_path / "ui" / "ruleset.png"
    screenshot.unlink()
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(
        "artifacts.ruleset: artifact_missing_or_unreadable" in error for error in errors
    )

    snapshot = synthetic_snapshot(tmp_path)
    _install_ruleset_screenshot(snapshot, tmp_path, b"not-a-png")
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("artifacts.ruleset: invalid_png_signature" in error for error in errors)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            _png_with_scanline(
                b"\x00\x00",
                color_type=3,
                before_idat=((b"PLTE", b"\x00\x00\x00"),),
            ),
            "unsupported_png_header",
        ),
        (
            _png_with_scanline(b"\x00\x00\x00\x00\x00", bit_depth=16),
            "unsupported_png_header",
        ),
        (
            _png_with_scanline(b"\x00\x00\x00\x00\x00", interlace=1),
            "unsupported_png_header",
        ),
        (_png_with_scanline(b"\x05\x00\x00\x00\x00"), "invalid_png_filter"),
        (
            _png_with_scanline(b"\x00\x00\x00", color_type=2),
            "png_image_data_size_mismatch",
        ),
        (
            _png_with_scanline(b"\x00\x00\x00\x00\x00", before_idat=((b"ABCD", b""),)),
            "unknown_critical_png_chunk",
        ),
        (_minimal_png() + b"trailing", "invalid_png_end"),
    ],
    ids=[
        "indexed-color",
        "sixteen-bit",
        "interlaced",
        "invalid-filter",
        "wrong-scanline-size",
        "unknown-critical",
        "trailing-bytes",
    ],
)
def test_png_rejects_malformed_screenshot_subset(
    content: bytes, reason: str, tmp_path: Path
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    _install_ruleset_screenshot(snapshot, tmp_path, content)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(f"artifacts.ruleset: {reason}" in error for error in errors)


def test_png_accepts_rgb_and_rgba_screenshot_subset() -> None:
    errors: list[str] = []
    validator._validate_png(
        _png_with_scanline(b"\x00\x10\x20\x30", color_type=2), "rgb", errors
    )
    validator._validate_png(_minimal_png(), "rgba", errors)
    assert errors == []


def test_attestation_rejects_path_escape_and_self_asserted_run_id(
    tmp_path: Path,
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    attestation = _read_attestation(tmp_path)
    attestation["artifacts"]["ruleset"]["path"] = "../outside.png"
    _write_attestation(snapshot, tmp_path, attestation)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(
        "artifacts.ruleset.path: invalid_relative_artifact_path" in error
        for error in errors
    )

    snapshot = synthetic_snapshot(tmp_path)
    attestation = _read_attestation(tmp_path)
    attestation["run_id"] = 123456
    _write_attestation(snapshot, tmp_path, attestation)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("attestation.document: unexpected_field" in error for error in errors)


def test_attestation_binds_complete_snapshot_digest(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    snapshot["aws"]["region"] = "us-east-2"
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(
        "snapshot_readback_sha256: snapshot_digest_mismatch" in error
        for error in errors
    )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (
            "ruleset_bypass_empty",
            False,
            "ruleset_bypass_empty: ui_confirmation_required",
        ),
        (
            "environment_admin_bypass_disabled",
            {"asaas-build": False, "asaas-production-deployment": True},
            "environment_admin_bypass_disabled.asaas-build: ui_confirmation_required",
        ),
        (
            "environment_branch_policy_names",
            {"asaas-build": ["release/*"], "asaas-production-deployment": ["main"]},
            "environment_branch_policy_names.asaas-build: exact_branch_evidence_required",
        ),
        (
            "environment_branch_only_no_tags",
            {"asaas-build": True, "asaas-production-deployment": False},
            "environment_branch_only_no_tags.asaas-production-deployment: ui_confirmation_required",
        ),
    ],
)
def test_human_ui_claims_fail_when_attestation_claims_are_weakened(
    field: str, value: Any, expected: str, tmp_path: Path
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    attestation = _read_attestation(tmp_path)
    attestation[field] = value
    _write_attestation(snapshot, tmp_path, attestation)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(expected in error for error in errors)


def test_attestation_rejects_mismatched_canonical_github_digest(tmp_path: Path) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    attestation = _read_attestation(tmp_path)
    attestation["github_readback_sha256"] = "a" * 64
    _write_attestation(snapshot, tmp_path, attestation)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(
        "github_readback_sha256: readback_digest_mismatch" in error for error in errors
    )


def test_evidence_root_and_artifacts_require_private_posix_files(
    tmp_path: Path,
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    tmp_path.chmod(0o755)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("evidence_root_permissions_open" in error for error in errors)
    tmp_path.chmod(0o700)

    snapshot = synthetic_snapshot(tmp_path)
    screenshot = tmp_path / "ui" / "ruleset.png"
    screenshot.chmod(0o644)
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any("artifact_permissions_open" in error for error in errors)


def test_evidence_root_rejects_symlink_and_artifacts_reject_links(
    tmp_path: Path,
) -> None:
    symlink_root = tmp_path / "symlink-root"
    symlink_root.mkdir(mode=0o700)
    snapshot = synthetic_snapshot(symlink_root)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(symlink_root, target_is_directory=True)
    errors = validator.validate_snapshot(snapshot, evidence_root=linked_root)
    assert any("evidence_root_symlink" in error for error in errors)

    symlink_snapshot = synthetic_snapshot(symlink_root)
    screenshot = symlink_root / "ui" / "ruleset.png"
    backup = symlink_root / "ui" / "ruleset-real.png"
    screenshot.rename(backup)
    screenshot.symlink_to(backup.name)
    errors = validator.validate_snapshot(symlink_snapshot, evidence_root=symlink_root)
    assert any("artifact_symlink" in error for error in errors)

    hardlink_root = tmp_path / "hardlink-root"
    hardlink_root.mkdir(mode=0o700)
    hardlink_snapshot = synthetic_snapshot(hardlink_root)
    screenshot = hardlink_root / "ui" / "ruleset.png"
    os.link(screenshot, hardlink_root / "ui" / "duplicate.png")
    errors = validator.validate_snapshot(hardlink_snapshot, evidence_root=hardlink_root)
    assert any("artifact_hardlink" in error for error in errors)


def test_grant_operation_enum_is_case_sensitive_and_complete() -> None:
    errors: list[str] = []
    assert validator._validate_grant_operations(
        ["DeriveSharedSecret"], "ops", errors
    ) == ["DeriveSharedSecret"]
    assert errors == []
    assert validator._validate_grant_operations(["sign"], "ops", errors) == ["sign"]
    assert "ops: unknown_or_case_mismatched_grant_operation" in errors
    assert "RevokeGrant" not in validator.KMS_GRANT_OPERATIONS


def test_readback_cli_validates_snapshot_and_never_prints_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "snapshot.json"
    snapshot = synthetic_snapshot(tmp_path)
    artifact_ref = snapshot["aws"]["kms_authorization_inventory"][
        "describe_regions_artifact"
    ]
    source = json.loads((tmp_path / artifact_ref["path"]).read_text())
    monkeypatch.setattr(
        validator,
        "PINNED_DESCRIBE_REGIONS_RESPONSE_SHA256",
        source["response_sha256"],
    )
    evidence.write_text(json.dumps(snapshot), encoding="utf-8")
    evidence.chmod(0o600)
    assert validator.main(["--evidence", str(evidence)]) == 0
    assert "valid" in capsys.readouterr().out

    snapshot["aws"]["ecr"]["repository_uri"] = "sentinel-secret-do-not-print"
    evidence.write_text(json.dumps(snapshot), encoding="utf-8")
    assert validator.main(["--evidence", str(evidence)]) == 1
    captured = capsys.readouterr()
    assert "snapshot.aws.ecr.repository_uri" in captured.err
    assert "sentinel-secret-do-not-print" not in captured.err


def test_readback_cli_rejects_open_snapshot_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "snapshot.json"
    evidence.write_text(json.dumps(synthetic_snapshot(tmp_path)), encoding="utf-8")
    evidence.chmod(0o644)
    assert validator.main(["--evidence", str(evidence)]) == 2
    assert "snapshot_permissions_open" in capsys.readouterr().err


def test_readback_cli_rejects_duplicate_snapshot_json_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "snapshot.json"
    evidence.write_text(
        '{"schema_version":"first","schema_version":"second"}',
        encoding="utf-8",
    )
    evidence.chmod(0o600)
    assert validator.main(["--evidence", str(evidence)]) == 2
    assert "unreadable_or_invalid_json" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("artifact_kind", "expected_error"),
    [
        ("attestation", "invalid_attestation_json"),
        ("describe_regions", "invalid_describe_regions_json"),
    ],
)
def test_json_artifacts_reject_duplicate_object_keys(
    tmp_path: Path, artifact_kind: str, expected_error: str
) -> None:
    snapshot = synthetic_snapshot(tmp_path)
    if artifact_kind == "attestation":
        descriptor = snapshot["github"]["ui_evidence"]["attestation"]
        path = tmp_path / descriptor["path"]
        duplicate_key = b'"schema_version":"duplicate","schema_version":'
        key_marker = b'"schema_version":'
        raw = path.read_bytes().replace(key_marker, duplicate_key, 1)
    else:
        descriptor = snapshot["aws"]["kms_authorization_inventory"][
            "describe_regions_artifact"
        ]
        path = tmp_path / descriptor["path"]
        key_marker = b'"operation":'
        raw = path.read_bytes().replace(
            key_marker, b'"operation":"duplicate","operation":', 1
        )
    path.write_bytes(raw)
    path.chmod(0o600)
    descriptor["sha256"] = hashlib.sha256(raw).hexdigest()
    errors = validator.validate_snapshot(snapshot, evidence_root=tmp_path)
    assert any(expected_error in error for error in errors)


def test_strict_json_loader_rejects_duplicate_keys_at_every_depth() -> None:
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        validator._loads_json_strict('{"outer":{"value":1,"value":2}}')


def test_template_check_rejects_trust_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    template = validator._load_template(TEMPLATE_PATH)
    template["Resources"]["BuildRole"]["Properties"]["AssumeRolePolicyDocument"][
        "Statement"
    ][0]["Condition"]["StringEquals"][
        "token.actions.githubusercontent.com:sub"
    ] = "repo:neurostackinfra/*"
    mutated_path = tmp_path / "mutated-template.yaml"
    mutated_path.write_text(json.dumps(template), encoding="utf-8")
    assert validator.main(["--check-template", "--template", str(mutated_path)]) == 1
    assert "wildcard_trust" in capsys.readouterr().err


@pytest.mark.parametrize(
    "mutate",
    [
        lambda template: template["Resources"]["BuildRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][1].update(Action=["ecs:UpdateService"]),
        lambda template: template["Resources"]["DeployRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][0].update(Resource="*"),
        lambda template: template["Resources"]["DeployRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][2].update(Condition={}),
        lambda template: template["Resources"]["ExistingTaskRoleSigningPermissions"][
            "Properties"
        ]["PolicyDocument"]["Statement"][0].update(
            Resource=[{"Fn::GetAtt": ["BuildAttestationSigningKey", "Arn"]}]
        ),
        lambda template: template["Resources"]["ExistingTaskRoleSigningPermissions"][
            "Properties"
        ]["PolicyDocument"]["Statement"][0].update(Action="kms:*"),
        lambda template: template["Resources"]["BuildRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][4].update(Resource="*"),
        lambda template: template["Resources"]["BuildRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][4].update(Condition={"StringLike": {"s3:prefix": "slots/*"}}),
        lambda template: template["Resources"]["BuildRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][4].update(Condition={"StringEquals": {"s3:prefix": "slots/"}}),
        lambda template: template["Resources"]["BuildRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][5].update(
            Resource={
                "Fn::Sub": "${BootstrapSlotLedgerBucket.Arn}/slots/deployment-bootstrap.json"
            }
        ),
        lambda template: template["Resources"]["DeployRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][5].update(
            Resource={
                "Fn::Sub": "${BootstrapSlotLedgerBucket.Arn}/slots/build-bootstrap.json"
            }
        ),
        lambda template: template["Resources"]["DeployRolePermissions"]["Properties"][
            "PolicyDocument"
        ]["Statement"][5].update(
            Resource={"Fn::Sub": "${BootstrapSlotLedgerBucket.Arn}/*"}
        ),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"][
            "Properties"
        ]["ObjectLockConfiguration"]["Rule"]["DefaultRetention"].update(Years=30),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"][
            "Properties"
        ].update(ObjectLockEnabled=False),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"][
            "Properties"
        ].pop("ObjectLockConfiguration"),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"][
            "Properties"
        ].update(VersioningConfiguration={"Status": "Suspended"}),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"][
            "Properties"
        ]["PublicAccessBlockConfiguration"].update(RestrictPublicBuckets=False),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"][
            "Properties"
        ]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0][
            "ServerSideEncryptionByDefault"
        ].update(
            SSEAlgorithm="aws:kms"
        ),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"][
            "Properties"
        ].update(LifecycleConfiguration={"Rules": [{"Status": "Enabled"}]}),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucketPolicy"][
            "Properties"
        ]["PolicyDocument"]["Statement"].pop(1),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucketPolicy"][
            "Properties"
        ]["PolicyDocument"]["Statement"][0].update(Condition={}),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucketPolicy"][
            "Properties"
        ]["PolicyDocument"]["Statement"][5].update(Action="s3:DeleteObject"),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"].update(
            DeletionPolicy="Delete"
        ),
        lambda template: template["Resources"]["BootstrapSlotLedgerBucket"].update(
            UpdateReplacePolicy="Delete"
        ),
        lambda template: template["Resources"][
            "BootstrapSlotLedgerBucketPolicy"
        ].update(DeletionPolicy="Delete"),
        lambda template: template["Resources"][
            "BootstrapSlotLedgerBucketPolicy"
        ].update(UpdateReplacePolicy="Delete"),
        lambda template: template["Resources"].update(
            UnexpectedLedger="AWS::S3::Bucket"
        ),
        lambda template: template["Resources"]["ProducerReceiptSigningKey"][
            "Properties"
        ].update(Enabled=False),
        lambda template: template["Outputs"].pop("ProducerGrantSigningKeyArn"),
        lambda template: template["Outputs"].update(UnexpectedOutput={"Value": "x"}),
        lambda template: template["Resources"]["DeployRole"].update(
            Condition="SkipDeployRole"
        ),
        lambda template: template.update(Transform="AWS::Serverless-2016-10-31"),
        lambda template: template.update(UnrecognizedTopLevelSection={"x": "y"}),
    ],
    ids=[
        "build-cannot-update-ecs",
        "register-scope-exact-task-family",
        "pass-role-service-condition-required",
        "task-role-signs-only-grant-and-receipt",
        "task-role-no-kms-action-wildcard",
        "build-list-no-broad-resource",
        "build-list-no-stringlike-prefix",
        "build-list-exact-build-prefix",
        "build-write-cannot-cross-role-key",
        "deploy-write-cannot-cross-role-key",
        "deploy-write-cannot-be-broad",
        "ledger-lock-100-year-retention",
        "ledger-object-lock-enabled",
        "ledger-object-lock-configuration-required",
        "ledger-versioning-enabled",
        "ledger-public-access-block",
        "ledger-encrypted-at-rest",
        "ledger-no-expiration-lifecycle",
        "ledger-conditional-put-required",
        "ledger-tls-only-policy",
        "ledger-delete-and-delete-version-denied",
        "ledger-bucket-retained",
        "ledger-bucket-replacement-retained",
        "ledger-policy-retained",
        "ledger-policy-replacement-retained",
        "unexpected-resource-rejected",
        "key-must-remain-enabled",
        "all-key-arns-output",
        "unexpected-output-rejected",
        "roles-cannot-be-conditionally-omitted",
        "transform-rejected",
        "unknown-top-level-field-rejected",
    ],
)
def test_template_policy_mutations_fail_closed(
    mutate: Callable[[dict[str, Any]], Any],
) -> None:
    template = copy.deepcopy(validator._load_template(TEMPLATE_PATH))
    mutate(template)
    assert validator._template_static_errors(template)


def test_template_checker_rejects_malformed_sections_without_crashing() -> None:
    template = copy.deepcopy(validator._load_template(TEMPLATE_PATH))
    template["Resources"]["BuildRole"]["Properties"] = "malformed"
    template["Rules"] = []
    assert validator._template_static_errors(template)
