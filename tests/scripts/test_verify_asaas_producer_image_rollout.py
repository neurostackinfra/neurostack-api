from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

import scripts.asaas_producer_rollout_evidence as rollout_evidence
import scripts.verify_asaas_producer_image_rollout as rollout
from scripts.asaas_producer_rollout_evidence import (
    ENCRYPTED_READBACK_MARKER,
    EvidenceError,
    _task_readiness_check,
    _validate_database_task_ip_binding,
    build_replacement_task_definition,
    decrypt_operator_snapshot,
    encrypt_operator_snapshot,
    generate_post_readback_key,
    parse_post_readback_comment,
    validate_before_after,
    validate_operator_readback,
    validate_pre_oidc_cohort_confirmation,
    validate_registered_task_definition,
    validate_task_definition_template,
)
from scripts.validate_asaas_registration_database_readiness import (
    main as validate_database_readiness_cli,
)
from scripts.verify_asaas_producer_image_rollout import (
    SERVICE_ARN,
    RolloutError,
    _public_key_fingerprint,
    compare_rollout_readbacks,
    envelope_digest,
    sanitize_aws_readbacks,
    validate_deployment_snapshot,
    validate_registration,
    validate_signed_attestation,
)
from src.services.billing_signed_envelope import (
    ALGORITHM,
    PURPOSE_BY_TYPE,
    TrustEntry,
    build_envelope,
)

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:388460142431:secret:neurostack-db-a1b2c3"
VERSION_ID = "12345678-1234-5678-1234-567812345678"
DIGEST = "sha256:" + "a" * 64
IMAGE_URI = "388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack/asaas-producer"
TASK_ROLE = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-task"
EXECUTION_ROLE = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-execution"


class P256Signer:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())

    def sign_digest(self, key_arn: str, digest: bytes) -> bytes:
        assert key_arn.endswith("01234567-89ab-cdef-0123-456789abcdef")
        return self.key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    @property
    def public_pem(self) -> str:
        return (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )


def _trust_env(signer: P256Signer) -> tuple[dict[str, str], dict[str, str], TrustEntry]:
    subject = (
        "repo:neurostackinfra@340698182/neurostack-api@1152335815:"
        "environment:asaas-build:ref:refs/heads/main:workflow_ref:"
        "neurostackinfra/neurostack-api/.github/workflows/"
        "asaas-build-attestation.yml@refs/heads/main"
    )
    env = {
        "BILLING_PRODUCER_BUILD_ISSUER": "github-actions:neurostack-build",
        "BILLING_PRODUCER_BUILD_KMS_KEY_ARN": "arn:aws:kms:us-east-1:388460142431:key/01234567-89ab-cdef-0123-456789abcdef",
        "BILLING_PRODUCER_BUILD_AUDIENCE": "billing-api:producer-build-attestation",
        "BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM": signer.public_pem,
        "BILLING_PRODUCER_BUILD_OIDC_SUBJECT": subject,
        "BILLING_PRODUCER_ECR_REPOSITORY_URI": IMAGE_URI,
        "INPUT_OPERATION_ID": str(uuid4()),
    }
    entry = TrustEntry(
        issuer=env["BILLING_PRODUCER_BUILD_ISSUER"],
        kid=env["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
        environment="staging",
        audience=env["BILLING_PRODUCER_BUILD_AUDIENCE"],
        type="billing_build_attestation.v1",
        purpose=PURPOSE_BY_TYPE["billing_build_attestation.v1"],
        schema_version="v1",
        algorithm=ALGORITHM,
        public_key_pem=signer.public_pem,
        repository_id="1152335815",
        repository_name="neurostackinfra/neurostack-api",
        workflow_path=".github/workflows/asaas-build-attestation.yml",
        workflow_ref="refs/heads/main",
        oidc_issuer="https://token.actions.githubusercontent.com",
        oidc_subject=subject,
    )
    trust = {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": "v1",
        "environment": "staging",
        "type": "billing_build_attestation.v1",
        "purpose": "billing-build-attestation.v1",
        "algorithm": "ECDSA_SHA_256",
        "issuer": entry.issuer,
        "kid": entry.kid,
        "audience": entry.audience,
        "public_key_fingerprint_sha256": _public_key_fingerprint(signer.public_pem),
        "repository_id": "1152335815",
        "repository_name": "neurostackinfra/neurostack-api",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "oidc_subject": subject,
    }
    return env, trust, entry


def _signed_envelope(signer: P256Signer, entry: TrustEntry) -> dict[str, object]:
    payload = {
        "attestation_type": "billing_build_attestation.v1",
        "attestation_id": str(uuid4()),
        "repository_id": "1152335815",
        "repository_name": "neurostackinfra/neurostack-api",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "workflow_run_id": 8123456789,
        "app_image_digest": DIGEST,
        "bundle_digest": "b" * 64,
        "commit_sha": "c" * 64,
        "tree_sha": "d" * 64,
        "workflow_id": "github-actions:1152335815:.github/workflows/asaas-build-attestation.yml",
        "immutable_artifact_reference": f"{IMAGE_URI}@{DIGEST}",
    }
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return build_envelope(
        entry=entry,
        payload=payload,
        operation_id=str(uuid4()),
        issued_at=now,
        expires_at=now + timedelta(days=7),
        signer=signer,
    )


def _snapshot() -> dict[str, object]:
    task_definition_arn = (
        "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:41"
    )
    task_arn = "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/active-1"
    environment = [
        {"name": "ENVIRONMENT", "value": "staging"},
        {"name": "HOST", "value": "0.0.0.0"},
        {"name": "LLM_MODEL", "value": "model-1"},
        {"name": "LOG_FORMAT", "value": "json"},
        {"name": "PORT", "value": "8000"},
        {"name": "QDRANT_HOST", "value": "qdrant.private"},
        {"name": "BILLING_ROLLOUT_STATE", "value": "disabled"},
        {"name": "BILLING_TOPUP_COMMERCIAL_ENABLED", "value": "false"},
    ]
    for name in ("GRANT", "BUILD", "RECEIPT"):
        environment.extend(
            [
                {
                    "name": f"BILLING_PRODUCER_{name}_KID",
                    "value": f"arn:aws:kms:us-east-1:388460142431:key/{name.lower()}",
                },
                {
                    "name": f"BILLING_PRODUCER_{name}_PUBLIC_KEY",
                    "value": f"public-{name.lower()}",
                },
            ]
        )
    return {
        "service": {
            "serviceArn": SERVICE_ARN,
            "serviceName": "neurostack-staging-app",
            "status": "ACTIVE",
            "desiredCount": 1,
            "runningCount": 1,
            "taskDefinition": task_definition_arn,
            "loadBalancers": [
                {
                    "targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:388460142431:targetgroup/neurostack-app/0123456789abcdef",
                    "containerName": "app",
                    "containerPort": 8000,
                }
            ],
            "deployments": [
                {
                    "id": "ecs-svc/1234567890123456789",
                    "status": "PRIMARY",
                    "taskDefinition": task_definition_arn,
                    "rolloutState": "COMPLETED",
                }
            ],
        },
        "tasks": [
            {
                "taskArn": task_arn,
                "taskDefinitionArn": task_definition_arn,
                "lastStatus": "RUNNING",
                "desiredStatus": "RUNNING",
                "createdAt": "2026-10-16T10:02:00.000Z",
                "startedAt": "2026-10-16T10:02:05.000Z",
                "privateIp": "10.0.1.7",
                "containers": [
                    {
                        "name": "app",
                        "lastStatus": "RUNNING",
                        "healthStatus": "HEALTHY",
                        "imageDigest": DIGEST,
                    }
                ],
            }
        ],
        "task_definition": {
            "taskDefinitionArn": task_definition_arn,
            "family": "neurostack-staging-app",
            "status": "ACTIVE",
            "taskRoleArn": TASK_ROLE,
            "executionRoleArn": EXECUTION_ROLE,
            "containerDefinitions": [
                {
                    "name": "app",
                    "image": f"{IMAGE_URI}@{DIGEST}",
                    "environment": environment,
                    "secrets": [
                        {
                            "name": "DATABASE_URL",
                            "valueFrom": f"{SECRET_ARN}:database_url:AWSCURRENT:",
                        }
                    ],
                    "healthCheck": {
                        "command": [
                            "CMD-SHELL",
                            "curl -fsS http://127.0.0.1:8080/ready",
                        ]
                    },
                }
            ],
        },
        "ecr": {"imageDetails": [{"imageDigest": DIGEST}]},
        "secret_description": {
            "ARN": SECRET_ARN,
            "LastChangedDate": "2026-10-16T10:00:00.000Z",
        },
        "secret_versions": {
            "ARN": SECRET_ARN,
            "Versions": [{"VersionId": VERSION_ID, "VersionStages": ["AWSCURRENT"]}],
        },
    }


def _expected_environment() -> dict[str, str]:
    result = {
        "BILLING_ROLLOUT_STATE": "disabled",
        "BILLING_TOPUP_COMMERCIAL_ENABLED": "false",
    }
    for name in ("GRANT", "BUILD", "RECEIPT"):
        result[f"BILLING_PRODUCER_{name}_KID"] = (
            f"arn:aws:kms:us-east-1:388460142431:key/{name.lower()}"
        )
        result[f"BILLING_PRODUCER_{name}_PUBLIC_KEY"] = f"public-{name.lower()}"
    return result


def _validate_snapshot(raw: dict[str, object]) -> dict[str, object]:
    return validate_deployment_snapshot(
        raw,
        expected_digest=DIGEST,
        expected_image_uri=IMAGE_URI,
        expected_task_role_arn=TASK_ROLE,
        expected_execution_role_arn=EXECUTION_ROLE,
        expected_secret_arn=SECRET_ARN,
        expected_secret_json_key="database_url",
        expected_secret_version_id=VERSION_ID,
        expected_task_environment=_expected_environment(),
        phase="after",
    )


def _operator_readback(
    *,
    phase: str = "before",
    database_task_ips: list[str] | None = None,
    database_readiness: dict[str, object] | None = None,
) -> dict[str, object]:
    if database_task_ips is None:
        database_task_ips = ["10.0.1.7"]
    snapshot = _snapshot()
    old_digest = "sha256:" + "b" * 64
    if phase == "before":
        snapshot["tasks"][0]["containers"][0]["imageDigest"] = old_digest
        snapshot["task_definition"]["containerDefinitions"][0][
            "image"
        ] = f"{IMAGE_URI}@{old_digest}"
        snapshot["task_definition"]["containerDefinitions"][0].pop("healthCheck", None)
        snapshot["task_definition"]["containerDefinitions"][0]["environment"] = [
            row
            for row in snapshot["task_definition"]["containerDefinitions"][0][
                "environment"
            ]
            if row["name"]
            in {"ENVIRONMENT", "HOST", "LLM_MODEL", "LOG_FORMAT", "PORT", "QDRANT_HOST"}
        ]
        snapshot["tasks"][0]["containers"][0]["healthStatus"] = "UNKNOWN"
        snapshot["ecr"]["imageDetails"] = [{"imageDigest": old_digest}]
    else:
        snapshot["task_definition"]["containerDefinitions"][0].pop("healthCheck", None)
        snapshot["tasks"][0]["containers"][0]["healthStatus"] = "UNKNOWN"
    snapshot["service"]["loadBalancers"] = [
        {
            "targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:388460142431:targetgroup/neurostack-app/0123456789abcdef",
            "containerName": "app",
            "containerPort": 8000,
        }
    ]
    checked = datetime.now(timezone.utc).replace(microsecond=0)
    operation_id = str(uuid4())
    attestation_id = str(uuid4())
    artifact = {
        "build_workflow_run_id": "8123456789",
        "attestation_id": attestation_id,
        "app_image_digest": DIGEST,
        "signed_envelope_sha256": "a" * 64,
    }
    raw = {
        "schema_version": "asaas_producer_run_bound_readback.v1",
        "phase": phase,
        "run_id": "9123456789",
        "run_attempt": "1",
        "workflow_path": ".github/workflows/asaas-producer-image-deploy.yml",
        "ref": "refs/heads/main",
        "commit_sha": "c" * 40,
        "operation_id": operation_id,
        "mode": "bootstrap",
        "checked_at_utc": checked.isoformat().replace("+00:00", "Z"),
        "operator_login": "operator-admin",
        "template_sha256": "d" * 64,
        "producer_environment_sha256": "e" * 64,
        "artifact": artifact,
        "database_readiness": (
            database_readiness
            if database_readiness is not None
            else {
                "schema": "asaas_registration_database_readiness.v1",
                "task_ips": database_task_ips,
                "task_ip_count": len(database_task_ips),
                "matched_task_ip_count": len(database_task_ips),
                "runtime_role_count": 1,
                "api_runtime_role": "neurostack_api_runtime",
                "required_columns_present": True,
                "required_constraints_present": True,
                "required_privileges_present": True,
                "row_lock_privilege_present": True,
                "readiness_ok": True,
            }
        ),
        "target_group": {
            "target_group_arn": snapshot["service"]["loadBalancers"][0][
                "targetGroupArn"
            ],
            "source_apis": ["DescribeTargetGroups", "DescribeTargetHealth"],
            "checked_at_utc": checked.isoformat().replace("+00:00", "Z"),
            "health_check_enabled": True,
            "health_check_path": "/health",
            "healthy_count": 1,
            "unhealthy_count": 0,
            "targets": [{"id": "10.0.1.7", "port": 8000, "health_state": "healthy"}],
        },
        "readiness_probe": {
            "source": "operator_attested_vpc_http_probe",
            "method": "GET",
            "path": "/ready",
            "checked_at_utc": checked.isoformat().replace("+00:00", "Z"),
            "targets": [{"id": "10.0.1.7", "status_code": 200, "ready": True}],
        },
        "snapshot": snapshot,
    }
    checked_raw = validate_operator_readback(
        raw,
        phase=phase,
        expected_run_id="9123456789",
        expected_run_attempt="1",
        expected_sha="c" * 40,
        expected_operation_id=operation_id,
        mode="bootstrap",
        artifact_tuple=artifact,
        template_sha256="d" * 64,
        producer_environment_sha256="e" * 64,
        expected_base_environment_sha256={
            name: hashlib.sha256(value.encode("utf-8")).hexdigest()
            for name, value in {
                "ENVIRONMENT": "staging",
                "HOST": "0.0.0.0",
                "LLM_MODEL": "model-1",
                "LOG_FORMAT": "json",
                "PORT": "8000",
                "QDRANT_HOST": "qdrant.private",
            }.items()
        },
        expected_operator_login="operator-admin",
        operator_permission="admin",
        expected_environment=_expected_environment(),
        expected_task_role_arn=TASK_ROLE,
        expected_execution_role_arn=EXECUTION_ROLE,
        expected_image_uri=IMAGE_URI,
        expected_secret_arn=SECRET_ARN,
        expected_secret_json_key="database_url",
        expected_secret_version_id=VERSION_ID,
        earliest_allowed_at=checked - timedelta(seconds=1),
        latest_allowed_at=checked + timedelta(seconds=1),
    )
    return raw | {"checked": checked_raw}


def test_signed_artifact_checks_signature_trust_tuple_and_group4_tuple() -> None:
    signer = P256Signer()
    env, trust, entry = _trust_env(signer)
    envelope = _signed_envelope(signer, entry)
    digest = envelope_digest(envelope)
    attestation_id = envelope["payload"]["attestation_id"]
    result = validate_signed_attestation(
        envelope,
        trust_readback=trust,
        expected_build_run_id="8123456789",
        expected_attestation_id=attestation_id,
        expected_digest=DIGEST,
        expected_envelope_sha256=digest,
        env=env,
    )
    assert result["attestation_id"] == attestation_id
    assert result["signed_envelope_sha256"] == digest
    tampered = copy.deepcopy(envelope)
    tampered["payload"]["app_image_digest"] = "sha256:" + "e" * 64
    with pytest.raises(
        RolloutError, match="signed_envelope_signature_or_schema_invalid"
    ):
        validate_signed_attestation(
            tampered,
            trust_readback=trust,
            expected_build_run_id="8123456789",
            expected_attestation_id=attestation_id,
            expected_digest=DIGEST,
            expected_envelope_sha256=digest,
            env=env,
        )


def test_registration_requires_exact_canonical_human_response() -> None:
    attestation = {"attestation_id": str(uuid4()), "attestation_digest": "a" * 64}
    result = validate_registration(
        {**attestation, "registered_at": "2026-10-16T10:00:00Z"},
        attestation,
        mode="registered",
    )
    assert result["registration_attestation_digest"] == "a" * 64
    with pytest.raises(RolloutError, match="registration_attestation_digest_mismatch"):
        validate_registration(
            {
                **attestation,
                "attestation_digest": "b" * 64,
                "registered_at": "2026-10-16T10:00:00Z",
            },
            attestation,
            mode="registered",
        )
    assert (
        validate_registration(None, attestation, mode="bootstrap")["registration"]
        == "pending_human_registration"
    )


def test_unexpected_environment_name_fails_closed_without_echoing_values() -> None:
    raw = _snapshot()
    raw["task_definition"]["containerDefinitions"][0]["environment"].append(
        {
            "name": "UNREPORTED_DATABASE_URL",
            "value": "postgresql://raw-user:raw-password@db.example/private",
        }
    )
    with pytest.raises(
        RolloutError, match="task_environment_name_set_mismatch"
    ) as error:
        _validate_snapshot(raw)
    assert "postgresql://" not in str(error.value)
    assert "raw-password" not in str(error.value)


def test_operator_alb_readiness_accepts_unknown_ecs_health_only_for_exact_live_ip_cohort() -> (
    None
):
    raw = _operator_readback()
    assert raw["checked"]["snapshot"]["readiness"] == "healthy"
    assert raw["checked"]["target_group"]["health_state_counts"] == {
        "healthy": 1,
        "draining": 0,
        "unused": 0,
    }
    assert raw["checked"]["target_group"]["source_apis"] == [
        "DescribeTargetGroups",
        "DescribeTargetHealth",
    ]
    assert (
        raw["checked"]["target_group"]["checked_at_utc"]
        == raw["checked"]["checked_at_utc"]
    )
    assert raw["checked"]["readiness_probe"]["ready_count"] == 1
    assert raw["checked"]["readiness_probe"]["path"] == "/ready"
    assert raw["checked"]["target_group"]["health_check_path"] == "/health"
    assert raw["checked"]["target_group"]["target_group_arn"].endswith(
        "0123456789abcdef"
    )
    assert raw["checked"]["snapshot"]["task_private_ips"] == ["10.0.1.7"]
    raw["target_group"]["targets"][0]["id"] = "10.0.1.8"
    with pytest.raises(EvidenceError, match="target_group_cohort_mismatch"):
        _operator_readback_with_raw(raw)


def test_readiness_cli_output_can_be_embedded_unchanged_in_readback_gate(
    tmp_path: Path,
) -> None:
    sql_row = {
        "task_ips": ["10.0.1.7"],
        "task_ip_count": 1,
        "matched_task_ip_count": 1,
        "runtime_role_count": 1,
        "api_runtime_role": "neurostack_api_runtime",
        "required_columns_present": True,
        "required_constraints_present": True,
        "required_privileges_present": True,
        "row_lock_privilege_present": True,
        "readiness_ok": True,
    }
    input_path = tmp_path / "readiness-row.json"
    output_path = tmp_path / "readiness-validated.json"
    input_path.write_text(json.dumps(sql_row), encoding="utf-8")

    assert (
        validate_database_readiness_cli(
            ["--input", str(input_path), "--output", str(output_path)]
        )
        == 0
    )
    validated_readiness = json.loads(output_path.read_text(encoding="utf-8"))
    assert validated_readiness["task_ips"] == ["10.0.1.7"]
    assert validated_readiness["readiness_ok"] is True
    assert "readiness" not in validated_readiness

    checked = _operator_readback(database_readiness=validated_readiness)["checked"]
    assert checked["database_runtime_role"] == "neurostack_api_runtime"


@pytest.mark.parametrize(
    "sql_task_ips",
    [["10.0.1.8"], ["10.0.1.7", "10.0.1.8"]],
)
def test_database_readiness_must_bind_exactly_to_every_snapshot_task_ip(
    sql_task_ips: list[str],
) -> None:
    with pytest.raises(EvidenceError, match="database_task_ip_cohort_mismatch"):
        _operator_readback(database_task_ips=sql_task_ips)


@pytest.mark.parametrize(
    ("sql_task_ips", "task_ips"),
    [
        (["10.0.1.7"], ["10.0.1.7", "10.0.1.8"]),
        (["10.0.1.7", "10.0.1.8", "10.0.1.9"], ["10.0.1.7", "10.0.1.8"]),
    ],
)
def test_database_readiness_rejects_omitted_or_extra_cohort_ips(
    sql_task_ips: list[str], task_ips: list[str]
) -> None:
    readiness = {
        "task_ips": sql_task_ips,
        "task_ip_count": len(sql_task_ips),
        "matched_task_ip_count": len(sql_task_ips),
    }
    snapshot_tasks = [{"privateIp": address} for address in task_ips]
    with pytest.raises(EvidenceError, match="database_task_ip_cohort_mismatch"):
        _validate_database_task_ip_binding(readiness, snapshot_tasks)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda row: row["targets"][0].update({"status_code": 503, "ready": False}),
            "task_readiness_probe_failed",
        ),
        (lambda row: row["targets"].pop(), "task_readiness_probe_targets_missing"),
        (
            lambda row: row.update({"response_body": "database credentials"}),
            "task_readiness_probe_invalid",
        ),
    ],
)
def test_task_readiness_probe_requires_http_200_for_exact_private_ip_cohort(
    mutate, reason: str
) -> None:
    checked_at = datetime.now(timezone.utc).replace(microsecond=0)
    raw = {
        "source": "operator_attested_vpc_http_probe",
        "method": "GET",
        "path": "/ready",
        "checked_at_utc": checked_at.isoformat().replace("+00:00", "Z"),
        "targets": [{"id": "10.0.1.7", "status_code": 200, "ready": True}],
    }
    mutate(raw)
    with pytest.raises(EvidenceError, match=reason):
        _task_readiness_check(
            raw,
            expected_task_ips=["10.0.1.7"],
            readback_checked_at=checked_at,
            earliest_allowed_at=checked_at - timedelta(seconds=1),
            latest_allowed_at=checked_at + timedelta(seconds=1),
        )


def _operator_readback_with_raw(value: dict[str, object]) -> None:
    from scripts.asaas_producer_rollout_evidence import _target_group_check

    snapshot = value["snapshot"]
    lb = snapshot["service"]["loadBalancers"][0]
    _target_group_check(
        value["target_group"],
        desired_count=1,
        target_group_arn=lb["targetGroupArn"],
        container_port=lb["containerPort"],
        expected_task_ips=[task["privateIp"] for task in snapshot["tasks"]],
        readback_checked_at=datetime.fromisoformat(
            value["checked_at_utc"].replace("Z", "+00:00")
        ),
        earliest_allowed_at=datetime.fromisoformat(
            value["checked_at_utc"].replace("Z", "+00:00")
        )
        - timedelta(minutes=1),
        latest_allowed_at=datetime.fromisoformat(
            value["checked_at_utc"].replace("Z", "+00:00")
        )
        + timedelta(minutes=1),
    )


def test_aws_projection_redacts_unknown_environment_values_and_never_copies_overrides() -> (
    None
):
    normalized = _snapshot()
    normalized["task_definition"]["containerDefinitions"][0]["environment"].append(
        {"name": "PUBLIC_API_BASE_URL", "value": "https://private.example.test"}
    )
    raw = {
        "service_response": {
            "services": [normalized["service"]],
            "failures": [],
        },
        "tasks_response": {
            "tasks": [
                {
                    **normalized["tasks"][0],
                    "attachments": [
                        {
                            "details": [
                                {"name": "privateIPv4Address", "value": "10.0.1.7"}
                            ]
                        }
                    ],
                },
            ],
            "failures": [],
        },
        "task_definition_response": {"taskDefinition": normalized["task_definition"]},
        "ecr_response": normalized["ecr"],
        "secret_description": normalized["secret_description"],
        "secret_versions": normalized["secret_versions"],
    }
    projected = sanitize_aws_readbacks(raw)
    encoded = json.dumps(projected, sort_keys=True)
    assert "https://private.example.test" not in encoded
    assert "DATABASE_URL" in encoded
    assert "SecretString" not in encoded
    assert projected["task_definition"]["containerDefinitions"][0]["environment"][
        -1
    ] == {
        "name": "PUBLIC_API_BASE_URL",
        "value": "<redacted>",
    }
    raw["secret_description"]["SecretString"] = "postgresql://never-copy"
    with pytest.raises(RolloutError, match="secret_value_field_forbidden"):
        sanitize_aws_readbacks(raw)


def test_before_and_after_readbacks_preserve_count_role_secret_and_trust_pins() -> None:
    before_raw = _snapshot()
    old_digest = "sha256:" + "b" * 64
    before_raw["tasks"][0]["containers"][0]["imageDigest"] = old_digest
    before_raw["task_definition"]["containerDefinitions"][0][
        "image"
    ] = f"{IMAGE_URI}@{old_digest}"
    before_raw["task_definition"]["containerDefinitions"][0]["environment"] = [
        row
        for row in before_raw["task_definition"]["containerDefinitions"][0][
            "environment"
        ]
        if row["name"]
        in {"ENVIRONMENT", "HOST", "LLM_MODEL", "LOG_FORMAT", "PORT", "QDRANT_HOST"}
    ]
    before_raw["ecr"]["imageDetails"] = [{"imageDigest": old_digest}]
    before = validate_deployment_snapshot(
        before_raw,
        expected_digest="current",
        expected_image_uri=IMAGE_URI,
        expected_task_role_arn=TASK_ROLE,
        expected_execution_role_arn=EXECUTION_ROLE,
        expected_secret_arn=SECRET_ARN,
        expected_secret_json_key="database_url",
        expected_secret_version_id=None,
        expected_task_environment=_expected_environment(),
        phase="before",
    )
    after = _validate_snapshot(_snapshot())
    result = compare_rollout_readbacks(before, after, expected_digest=DIGEST)
    assert result["previous_image_digest"] == old_digest
    assert result["active_image_digest"] == DIGEST
    assert result["active_primary_deployment_id"] == "ecs-svc/1234567890123456789"
    after["database_secret_version_id"] = "different-version"
    with pytest.raises(
        RolloutError, match="readback_database_secret_version_id_changed"
    ):
        compare_rollout_readbacks(before, after, expected_digest=DIGEST)


def test_snapshot_requires_primary_deployment_id_for_live_binding() -> None:
    raw = _snapshot()
    del raw["service"]["deployments"][0]["id"]
    with pytest.raises(RolloutError, match="service_primary_deployment_mismatch"):
        _validate_snapshot(raw)


def _cohort_summary(
    *, phase: str, task_suffix: str, primary_deployment_id: str
) -> dict[str, object]:
    snapshot = {
        "schema": "asaas_producer_image_rollout_readback.v1",
        "cluster_arn": "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging",
        "service_arn": SERVICE_ARN,
        "task_definition_arn": "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8",
        "primary_deployment_id": primary_deployment_id,
        "task_count": 2,
        "task_arns": [f"task-{task_suffix}-1", f"task-{task_suffix}-2"],
        "task_private_ips": ["10.0.1.7", "10.0.1.8"],
        "task_timestamps": [
            {
                "task_arn": f"task-{task_suffix}-1",
                "created_at": "2026-10-16T10:00:00Z",
                "started_at": "2026-10-16T10:00:05Z",
            },
            {
                "task_arn": f"task-{task_suffix}-2",
                "created_at": "2026-10-16T10:00:01Z",
                "started_at": "2026-10-16T10:00:06Z",
            },
        ],
        "runtime_fingerprint_sha256": "b" * 64,
        "base_environment_sha256": {"HOST": "c" * 64},
        "app_image_digest": "sha256:" + "d" * 64,
        "task_role_arn": TASK_ROLE,
        "execution_role_arn": EXECUTION_ROLE,
        "database_secret_arn": SECRET_ARN,
        "database_secret_json_key": "database_url",
        "database_secret_version_id": VERSION_ID,
        "readiness": "healthy",
        "commercial_gates": "disabled",
        "trust_pins": {"BILLING_PRODUCER_BUILD_KID": "key"},
    }
    return {
        "phase": phase,
        "operator_login": "operator-admin",
        "operator_permission": "admin",
        "template_sha256": "e" * 64,
        "base_environment_sha256": {"HOST": "c" * 64},
        "readiness": "ready",
        "readiness_probe": {"path": "/ready", "ready_count": 2},
        "target_group": {"healthy_count": 2},
        "snapshot": snapshot,
        "snapshot_sha256": "f" * 64,
    }


def test_pre_oidc_cohort_allows_changed_tasks_only_with_fresh_readiness_contract() -> (
    None
):
    before = _cohort_summary(
        phase="before", task_suffix="old", primary_deployment_id="deploy-old"
    )
    cohort = _cohort_summary(
        phase="cohort", task_suffix="fresh", primary_deployment_id="deploy-fresh"
    )
    result = validate_pre_oidc_cohort_confirmation(before, cohort)
    assert result["cohort_changed_since_predeploy"] is True
    assert result["primary_deployment_id"] == "deploy-fresh"

    cohort["readiness_probe"] = {"ready_count": 1}
    with pytest.raises(EvidenceError, match="cohort_confirmation_readiness_failed"):
        validate_pre_oidc_cohort_confirmation(before, cohort)


def test_pre_oidc_cohort_accepts_matching_normal_admin_login() -> None:
    before = _cohort_summary(
        phase="before", task_suffix="old", primary_deployment_id="deploy-old"
    )
    cohort = _cohort_summary(
        phase="cohort", task_suffix="fresh", primary_deployment_id="deploy-fresh"
    )
    before["operator_login"] = "lorenzo"
    cohort["operator_login"] = "lorenzo"
    before["operator_permission"] = "admin"
    cohort["operator_permission"] = "admin"

    result = validate_pre_oidc_cohort_confirmation(before, cohort)
    assert result["cohort_changed_since_predeploy"] is True


@pytest.mark.parametrize(
    ("before_login", "cohort_login", "before_permission", "cohort_permission"),
    [
        ("lorenzo", "lorenzo", "write", "admin"),
        ("lorenzo", "lorenzo", "admin", "write"),
        ("lorenzo", "another-admin", "admin", "admin"),
    ],
)
def test_pre_oidc_cohort_rejects_permission_or_login_mismatch(
    before_login: str,
    cohort_login: str,
    before_permission: str,
    cohort_permission: str,
) -> None:
    before = _cohort_summary(
        phase="before", task_suffix="old", primary_deployment_id="deploy-old"
    )
    cohort = _cohort_summary(
        phase="cohort", task_suffix="fresh", primary_deployment_id="deploy-fresh"
    )
    before["operator_login"] = before_login
    cohort["operator_login"] = cohort_login
    before["operator_permission"] = before_permission
    cohort["operator_permission"] = cohort_permission

    with pytest.raises(EvidenceError, match="cohort_confirmation_operator_mismatch"):
        validate_pre_oidc_cohort_confirmation(before, cohort)


def test_postdeploy_readback_accepts_matching_normal_admin_login() -> None:
    before = _operator_readback(phase="before")["checked"]
    after = _operator_readback(phase="after")["checked"]
    before["operator_login"] = "lorenzo"
    after["operator_login"] = "lorenzo"

    result = validate_before_after(before, after, expected_digest=DIGEST)
    assert result["verified"] is True


@pytest.mark.parametrize("phase", ["before", "after"])
def test_postdeploy_readback_rejects_non_admin_operator_permission(
    phase: str,
) -> None:
    before = _operator_readback(phase="before")["checked"]
    after = _operator_readback(phase="after")["checked"]
    (before if phase == "before" else after)["operator_permission"] = "write"

    with pytest.raises(EvidenceError, match="operator_readback_admin_mismatch"):
        validate_before_after(before, after, expected_digest=DIGEST)


def test_pre_oidc_cohort_rejects_secret_or_task_definition_drift() -> None:
    before = _cohort_summary(
        phase="before", task_suffix="same", primary_deployment_id="deploy-1"
    )
    cohort = _cohort_summary(
        phase="cohort", task_suffix="same", primary_deployment_id="deploy-1"
    )
    cohort["snapshot"]["database_secret_version_id"] = "different-version"
    with pytest.raises(
        EvidenceError,
        match="cohort_confirmation_database_secret_version_id_mismatch",
    ):
        validate_pre_oidc_cohort_confirmation(before, cohort)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda raw: raw["tasks"][0]["containers"][0].update(
                {"imageDigest": "sha256:" + "f" * 64}
            ),
            "active_task_digest_mismatch",
        ),
        (
            lambda raw: raw["tasks"][0]["containers"][0].update(
                {"healthStatus": "UNHEALTHY"}
            ),
            "app_container_not_healthy",
        ),
        (
            lambda raw: next(
                row
                for row in raw["task_definition"]["containerDefinitions"][0][
                    "environment"
                ]
                if row["name"] == "BILLING_ROLLOUT_STATE"
            ).update({"value": "enabled"}),
            "task_trust_or_commercial_gate_mismatch",
        ),
        (
            lambda raw: raw["task_definition"].update(
                {"taskRoleArn": "arn:aws:iam::388460142431:role/other"}
            ),
            "task_role_mismatch",
        ),
        (
            lambda raw: raw["secret_description"].update(
                {"SecretString": "must-never-be-read"}
            ),
            "database_secret_secret_value_field_forbidden",
        ),
        (
            lambda raw: raw["service"].update({"runningCount": 0}),
            "service_running_count_mismatch",
        ),
    ],
)
def test_unhealthy_or_sensitive_readbacks_fail_closed(mutate, reason: str) -> None:
    raw = copy.deepcopy(_snapshot())
    mutate(raw)
    with pytest.raises(RolloutError, match=reason):
        _validate_snapshot(raw)


def test_secret_version_change_after_task_pending_fails_closed() -> None:
    raw = _snapshot()
    raw["secret_description"]["LastChangedDate"] = "2026-10-16T10:02:02.000Z"
    with pytest.raises(
        RolloutError,
        match="database_secret_floating_secret_mapping_not_stable_before_task_pending",
    ):
        _validate_snapshot(raw)


def test_exact_secret_reference_parser_does_not_accept_raw_database_url() -> None:
    raw = _snapshot()
    raw["task_definition"]["containerDefinitions"][0]["secrets"][0][
        "valueFrom"
    ] = "postgresql://user:password@db.example/private"
    with pytest.raises(RolloutError, match="database_url_reference_invalid"):
        _validate_snapshot(raw)


def test_cli_help_is_available_without_credentials() -> None:
    import subprocess
    import sys

    script = (
        Path(__file__).resolve().parents[2]
        / "scripts/verify_asaas_producer_image_rollout.py"
    )
    result = subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "validate-snapshot" in result.stdout
    assert "https://" not in result.stdout


def _task_definition_template(signer: P256Signer) -> dict[str, object]:
    return {
        "schema_version": "asaas_producer_task_definition_template.v1",
        "source_task_definition_arn": "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8",
        "task_definition": {
            "family": "neurostack-staging-app",
            "taskRoleArn": TASK_ROLE,
            "executionRoleArn": EXECUTION_ROLE,
            "networkMode": "awsvpc",
            "requiresCompatibilities": ["FARGATE"],
            "cpu": "1024",
            "memory": "2048",
            "runtimePlatform": {
                "cpuArchitecture": "X86_64",
                "operatingSystemFamily": "LINUX",
            },
            "containerDefinitions": [
                {
                    "name": "app",
                    "image": f"{IMAGE_URI}:active",
                    "essential": True,
                    "environment": [
                        {"name": "ENVIRONMENT", "value": "staging"},
                        {"name": "HOST", "value": "0.0.0.0"},
                        {"name": "LLM_MODEL", "value": "model-1"},
                        {"name": "LOG_FORMAT", "value": "json"},
                        {"name": "PORT", "value": "8000"},
                        {"name": "QDRANT_HOST", "value": "qdrant.private"},
                    ],
                    "secrets": [
                        {"name": "DATABASE_URL", "valueFrom": SECRET_ARN},
                        {
                            "name": "REDIS_URL",
                            "valueFrom": "arn:aws:secretsmanager:us-east-1:388460142431:secret:redis-123456",
                        },
                        {
                            "name": "SECRET_KEY",
                            "valueFrom": "arn:aws:secretsmanager:us-east-1:388460142431:secret:secret-key-123456",
                        },
                        {
                            "name": "LLM_API_KEY",
                            "valueFrom": "arn:aws:secretsmanager:us-east-1:388460142431:secret:llm-key-123456",
                        },
                        {
                            "name": "BILLING_OPERATION_RECEIPT_SIGNING_KEY",
                            "valueFrom": "arn:aws:secretsmanager:us-east-1:388460142431:secret:receipt-key-123456",
                        },
                    ],
                    "portMappings": [
                        {"containerPort": 8000, "hostPort": 8000, "protocol": "tcp"}
                    ],
                },
                {
                    "name": "otel",
                    "image": "public.ecr.aws/otel/opentelemetry-collector:latest",
                    "essential": False,
                    "environment": [],
                    "secrets": [],
                },
            ],
        },
    }


def _set_group2_key_pins(monkeypatch: pytest.MonkeyPatch, signer: P256Signer) -> None:
    for kind in ("GRANT", "BUILD", "RECEIPT"):
        monkeypatch.setenv(
            f"BILLING_PRODUCER_{kind}_KMS_KEY_ARN",
            f"arn:aws:kms:us-east-1:388460142431:key/{kind.lower()}-01234567-89ab-cdef-0123-456789abcdef",
        )
        monkeypatch.setenv(f"BILLING_PRODUCER_{kind}_PUBLIC_KEY_PEM", signer.public_pem)


def test_private_task_definition_template_is_rebuilt_with_exact_pins_and_secret_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _task_definition_template(signer)
    validated = validate_task_definition_template(template)
    assert validated["source_task_definition_arn"].endswith(":8")
    assert validated["app_environment_names"] == sorted(
        ["ENVIRONMENT", "HOST", "LLM_MODEL", "LOG_FORMAT", "PORT", "QDRANT_HOST"]
    )
    pinned_db_ref = f"{SECRET_ARN}:::{VERSION_ID}"
    request = build_replacement_task_definition(
        template,
        image_uri=IMAGE_URI,
        app_digest=DIGEST,
        pinned_database_value_from=pinned_db_ref,
    )
    app = next(row for row in request["containerDefinitions"] if row["name"] == "app")
    assert app["image"] == f"{IMAGE_URI}@{DIGEST}"
    assert {row["name"] for row in app["environment"]} == {
        "ENVIRONMENT",
        "HOST",
        "LLM_MODEL",
        "LOG_FORMAT",
        "PORT",
        "QDRANT_HOST",
        "BILLING_ROLLOUT_STATE",
        "BILLING_TOPUP_COMMERCIAL_ENABLED",
        *(
            f"BILLING_PRODUCER_{kind}_{suffix}"
            for kind in ("GRANT", "BUILD", "RECEIPT")
            for suffix in ("KID", "PUBLIC_KEY")
        ),
    }
    secrets = {row["name"]: row["valueFrom"] for row in app["secrets"]}
    assert secrets["DATABASE_URL"] == pinned_db_ref
    assert (
        secrets["REDIS_URL"]
        == "arn:aws:secretsmanager:us-east-1:388460142431:secret:redis-123456"
    )
    assert (
        next(
            row["value"]
            for row in app["environment"]
            if row["name"] == "BILLING_TOPUP_COMMERCIAL_ENABLED"
        )
        == "false"
    )


def test_task_definition_template_rejects_response_fields_raw_db_url_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    response_field = _task_definition_template(signer)
    response_field["task_definition"]["status"] = "ACTIVE"
    with pytest.raises(EvidenceError, match="task_definition_response_field_forbidden"):
        validate_task_definition_template(response_field)
    raw_url = _task_definition_template(signer)
    raw_url["task_definition"]["containerDefinitions"][0]["secrets"][0][
        "valueFrom"
    ] = "postgresql://user:password@db.example/private"
    with pytest.raises(EvidenceError, match="task_secret_reference_invalid"):
        validate_task_definition_template(raw_url)
    secret_value = _task_definition_template(signer)
    secret_value["SecretString"] = "never-read"
    with pytest.raises(EvidenceError, match="secret_value_field_forbidden"):
        validate_task_definition_template(secret_value)


def test_registered_task_definition_summary_never_contains_environment_or_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _task_definition_template(signer)
    request = build_replacement_task_definition(
        template,
        image_uri=IMAGE_URI,
        app_digest=DIGEST,
        pinned_database_value_from=f"{SECRET_ARN}:::{VERSION_ID}",
    )
    response_definition = copy.deepcopy(request)
    response_definition.update(
        {
            "taskDefinitionArn": "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:9",
            "revision": 9,
            "status": "ACTIVE",
        }
    )
    response = {"taskDefinition": response_definition}
    summary = validate_registered_task_definition(
        response, request, expected_digest=DIGEST
    )
    encoded = json.dumps(summary, sort_keys=True)
    assert summary["task_definition_arn"].endswith(":9")
    assert "qdrant.private" not in encoded
    assert SECRET_ARN not in encoded
    assert "private" not in encoded


@pytest.mark.parametrize("phase", ["cohort", "after"])
def test_encrypted_readback_comment_binds_full_context_and_admin_identity(
    phase: str,
) -> None:
    private_pem, public_pem = generate_post_readback_key()
    context = {
        "run_id": "8123456789",
        "run_attempt": "1",
        "operation_id": str(uuid4()),
        "phase": phase,
        "artifact_sha256": "a" * 64,
        "ref": "refs/heads/main",
        "sha": "c" * 40,
        "job_id": "9212345678",
        "job_started_at": "2026-10-16T10:00:00.000Z",
    }
    snapshot = {
        "task_definition_arn": "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:9",
        "secret_arn": SECRET_ARN,
        "private_environment": {"QDRANT_HOST": "qdrant.private"},
    }
    envelope = encrypt_operator_snapshot(snapshot, public_pem, context)
    serialized = json.dumps(envelope, sort_keys=True)
    assert "qdrant.private" not in serialized
    assert SECRET_ARN not in serialized
    restored = decrypt_operator_snapshot(
        envelope, private_pem, expected_context=context
    )
    assert restored == snapshot
    assert envelope["snapshot_sha256"]
    comment = f"<!-- {ENCRYPTED_READBACK_MARKER}\n" + serialized + "\n-->"
    parsed = parse_post_readback_comment(
        comment,
        author_login="operator-admin",
        author_permission="admin",
        expected_operator_login="operator-admin",
        private_pem=private_pem,
        expected_context=context,
    )
    assert parsed == snapshot
    with pytest.raises(EvidenceError, match="post_readback_author_not_admin"):
        parse_post_readback_comment(
            comment,
            author_login="unprivileged",
            author_permission="write",
            expected_operator_login="operator-admin",
            private_pem=private_pem,
            expected_context=context,
        )
    wrong_job_context = {**context, "job_id": "9212345679"}
    with pytest.raises(EvidenceError, match="post_snapshot_envelope_context_mismatch"):
        decrypt_operator_snapshot(
            envelope, private_pem, expected_context=wrong_job_context
        )
    tampered = copy.deepcopy(envelope)
    tampered["ciphertext_b64"] = tampered["ciphertext_b64"][:-2] + "AA"
    with pytest.raises(EvidenceError, match="post_snapshot_decryption_failed"):
        decrypt_operator_snapshot(tampered, private_pem, expected_context=context)


# ---------------------------------------------------------------------------
# F1 path A: the D3 name sets, the source/target image repositories and the
# D3 environment value fingerprints (docs/runbooks/asaas-deployment.md, F1).
# ---------------------------------------------------------------------------

BASE_ENV_VALUES = {
    "ENVIRONMENT": "staging",
    "HOST": "0.0.0.0",
    "LLM_MODEL": "model-1",
    "LOG_FORMAT": "json",
    "PORT": "8000",
    "QDRANT_HOST": "qdrant.private",
}
BASE_SECRET_NAMES = {
    "DATABASE_URL",
    "REDIS_URL",
    "SECRET_KEY",
    "LLM_API_KEY",
    "BILLING_OPERATION_RECEIPT_SIGNING_KEY",
}
# Exactly the Group 9 D3 wiring names (infra/production/asaas-production-wiring.yaml);
# the values are illustrative and non-secret.
D3_ENV = {
    "ASAAS_WEBHOOK_URL": "https://staging.example.test/v1/billing/webhook/asaas",
    "ASAAS_CHECKOUT_RETURN_URL": "https://payments.staging.example.test/billing",
    "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": "operator@example.test",
    "BILLING_FACT_WORKER_ENABLED": "false",
}
# asaas-rollout-opening: the two staged flags (live step L2a on) and the
# AppConfig group (L7 on), as the planner renders them.
STAGED_FLAG_ENV = {
    "BILLING_FACT_TRANSPORT_ENABLED": "false",
    "CREDIT_HOT_PATH_ENABLED": "false",
}
APPCONFIG_ENV = {
    "APPCONFIG_APPLICATION": "neurostack-staging",
    "APPCONFIG_ENABLED": "true",
    "APPCONFIG_ENVIRONMENT": "production",
    "APPCONFIG_PROFILE": "financial-egress",
    "APPCONFIG_SOURCE": "sdk",
}
_D3_SECRET_PREFIX = "arn:aws:secretsmanager:us-east-1:388460142431:secret:neurostack/"
D3_SECRETS = {
    "ASAAS_API_KEY": _D3_SECRET_PREFIX + "staging/asaas-api-key-AbC123",
    "ASAAS_WEBHOOK_TOKEN": _D3_SECRET_PREFIX + "staging/asaas-webhook-token-DeF456",
    "BILLING_OBSERVABILITY_SMOKE_TOKEN": _D3_SECRET_PREFIX
    + "staging/billing-observability-smoke-token-mSR8Ux",
}
REGISTRY = "388460142431.dkr.ecr.us-east-1.amazonaws.com"
SOURCE_REPOSITORY = f"{REGISTRY}/neurostack-staging"
SOURCE_DIGEST = "sha256:" + "5" * 64
SOURCE_IMAGE = f"{SOURCE_REPOSITORY}@{SOURCE_DIGEST}"
REV8_TAGGED_IMAGE = f"{SOURCE_REPOSITORY}:asaas-gateway-staging-20261014-cfcb7f0b"
REV9_ARN = "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:9"
REV10_ARN = REV9_ARN.replace(":9", ":10")
PINNED_DATABASE_REFERENCE = f"{SECRET_ARN}:::{VERSION_ID}"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _app_container(definition: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = definition["containerDefinitions"]
    return next(row for row in rows if row["name"] == "app")


def _d3_template(signer: P256Signer, *, image: str = SOURCE_IMAGE) -> dict[str, Any]:
    """The protected template regenerated from a D3 revision 9 readback."""

    template = cast(dict[str, Any], _task_definition_template(signer))
    template["source_task_definition_arn"] = REV9_ARN
    app = _app_container(template["task_definition"])
    app["image"] = image
    app["environment"] += [{"name": n, "value": v} for n, v in D3_ENV.items()]
    app["secrets"] += [{"name": n, "valueFrom": v} for n, v in D3_SECRETS.items()]
    return template


def _rev8_template(signer: P256Signer) -> dict[str, Any]:
    template = cast(dict[str, Any], _task_definition_template(signer))
    _app_container(template["task_definition"])["image"] = REV8_TAGGED_IMAGE
    return template


def _drop_name(template: dict[str, Any], field: str, name: str) -> dict[str, Any]:
    app = _app_container(template["task_definition"])
    app[field] = [row for row in app[field] if row["name"] != name]
    return template


def _d3_snapshot(*, phase: str = "before", image: str | None = None) -> dict[str, Any]:
    """Serving D3 snapshot (before) or the attested replacement (after)."""

    raw = cast(dict[str, Any], _snapshot())
    app = _app_container(raw["task_definition"])
    app["environment"] += [{"name": n, "value": v} for n, v in D3_ENV.items()]
    app["secrets"] += [{"name": n, "valueFrom": v} for n, v in D3_SECRETS.items()]
    if phase == "before":
        app["environment"] = [
            row
            for row in app["environment"]
            if row["name"] in set(BASE_ENV_VALUES) | set(D3_ENV)
        ]
        app["image"] = SOURCE_IMAGE
        raw["tasks"][0]["containers"][0]["imageDigest"] = SOURCE_DIGEST
        raw["ecr"]["imageDetails"] = [{"imageDigest": SOURCE_DIGEST}]
    if image is not None:
        app["image"] = image
    return raw


def _drop_d3_environment(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a serving D3 snapshot into the revision 8 environment shape."""

    app = _app_container(raw["task_definition"])
    app["environment"] = [
        row for row in app["environment"] if row["name"] not in D3_ENV
    ]
    return raw


def _set_env(raw: dict[str, Any], name: str, value: str) -> dict[str, Any]:
    for row in _app_container(raw["task_definition"])["environment"]:
        if row["name"] == name:
            row["value"] = value
    return raw


def _validate_d3_snapshot(
    raw: dict[str, Any],
    *,
    phase: str = "before",
    expected_image_uri: str = IMAGE_URI,
    expected_base: dict[str, str] | None = None,
    expected_environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    return validate_deployment_snapshot(
        raw,
        expected_digest="current" if phase == "before" else DIGEST,
        expected_image_uri=expected_image_uri,
        expected_task_role_arn=TASK_ROLE,
        expected_execution_role_arn=EXECUTION_ROLE,
        expected_secret_arn=SECRET_ARN,
        expected_secret_json_key="database_url",
        expected_secret_version_id=VERSION_ID,
        expected_task_environment=(
            _expected_environment()
            if expected_environment is None
            else expected_environment
        ),
        expected_base_environment_sha256=expected_base,
        phase=phase,
    )


def _snapshot_of(
    definition: dict[str, Any], *, arn: str, running_digest: str
) -> dict[str, Any]:
    """A raw snapshot whose task definition is ``definition`` registered as ``arn``."""

    raw = cast(dict[str, Any], _snapshot())
    raw["service"]["taskDefinition"] = arn
    raw["service"]["deployments"][0]["taskDefinition"] = arn
    raw["tasks"][0]["taskDefinitionArn"] = arn
    raw["tasks"][0]["containers"][0]["imageDigest"] = running_digest
    raw["tasks"][0]["containers"][0]["healthStatus"] = "UNKNOWN"
    raw["ecr"]["imageDetails"] = [{"imageDigest": running_digest}]
    raw["task_definition"] = {
        **copy.deepcopy(definition),
        "taskDefinitionArn": arn,
        "status": "ACTIVE",
    }
    return raw


def _pinned_task_environment() -> dict[str, str]:
    expected = {
        "BILLING_ROLLOUT_STATE": "disabled",
        "BILLING_TOPUP_COMMERCIAL_ENABLED": "false",
    }
    for kind in ("GRANT", "BUILD", "RECEIPT"):
        expected[f"BILLING_PRODUCER_{kind}_KID"] = os.environ[
            f"BILLING_PRODUCER_{kind}_KMS_KEY_ARN"
        ]
        expected[f"BILLING_PRODUCER_{kind}_PUBLIC_KEY"] = os.environ[
            f"BILLING_PRODUCER_{kind}_PUBLIC_KEY_PEM"
        ]
    return expected


def test_validator_name_sets_are_the_revision_8_sets_plus_exactly_the_d3_names() -> (
    None
):
    assert rollout.BASE_ENVIRONMENT_NAMES == set(BASE_ENV_VALUES)
    assert rollout.D3_ENVIRONMENT_NAMES == set(D3_ENV)
    assert rollout.D3_SECRET_NAMES == set(D3_SECRETS)
    assert rollout.STAGED_FLAG_ENVIRONMENT_NAMES == set(STAGED_FLAG_ENV)
    assert rollout.APPCONFIG_ENVIRONMENT_NAMES == set(APPCONFIG_ENV)
    assert rollout.FINGERPRINTED_ENVIRONMENT_NAMES == (
        set(BASE_ENV_VALUES) | set(D3_ENV) | set(STAGED_FLAG_ENV) | set(APPCONFIG_ENV)
    )
    # Each stage all or none, a later one only on top of the earlier ones.
    assert rollout.ENVIRONMENT_NAME_SETS == (
        set(BASE_ENV_VALUES),
        set(BASE_ENV_VALUES) | set(D3_ENV),
        set(BASE_ENV_VALUES) | set(D3_ENV) | set(STAGED_FLAG_ENV),
        set(BASE_ENV_VALUES) | set(D3_ENV) | set(STAGED_FLAG_ENV) | set(APPCONFIG_ENV),
    )
    assert rollout_evidence._APP_ENVIRONMENT_NAME_SETS is rollout.ENVIRONMENT_NAME_SETS
    assert rollout_evidence._BASE_ENV_NAMES == set(BASE_ENV_VALUES)
    assert rollout_evidence._EXPECTED_APP_SECRET_NAMES == BASE_SECRET_NAMES
    assert rollout.SOURCE_IMAGE_REPOSITORY == SOURCE_REPOSITORY


@pytest.mark.parametrize(
    "stages",
    [
        (STAGED_FLAG_ENV,),
        (STAGED_FLAG_ENV, APPCONFIG_ENV),
    ],
)
def test_template_accepts_each_staged_set_and_fingerprints_its_values(
    monkeypatch: pytest.MonkeyPatch, stages: tuple[dict[str, str], ...]
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _d3_template(signer)
    added: dict[str, str] = {}
    for stage in stages:
        added.update(stage)
    _app_container(template["task_definition"])["environment"] += [
        {"name": n, "value": v} for n, v in added.items()
    ]
    validated = validate_task_definition_template(template)
    assert validated["app_environment_names"] == sorted(
        set(BASE_ENV_VALUES) | set(D3_ENV) | set(added)
    )
    assert validated["base_environment_sha256"] == {
        name: _sha(value)
        for name, value in {**BASE_ENV_VALUES, **D3_ENV, **added}.items()
    }


@pytest.mark.parametrize(
    "names",
    [
        # One staged flag without the other.
        ("CREDIT_HOT_PATH_ENABLED",),
        ("BILLING_FACT_TRANSPORT_ENABLED",),
        # The AppConfig group without the flags, or a part of it.
        tuple(APPCONFIG_ENV),
        (*STAGED_FLAG_ENV, "APPCONFIG_ENABLED", "APPCONFIG_SOURCE"),
        (*STAGED_FLAG_ENV, *APPCONFIG_ENV, "APPCONFIG_TOKEN"),
        (*STAGED_FLAG_ENV, *APPCONFIG_ENV, "APPCONFIG_ORIGIN"),
    ],
)
def test_template_refuses_a_partial_or_out_of_order_stage(
    monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _d3_template(signer)
    values = {**STAGED_FLAG_ENV, **APPCONFIG_ENV}
    _app_container(template["task_definition"])["environment"] += [
        {"name": name, "value": values.get(name, "x")} for name in names
    ]
    with pytest.raises(
        EvidenceError, match="task_definition_base_environment_names_mismatch"
    ):
        validate_task_definition_template(template)


def test_template_accepts_exactly_the_d3_names_and_fingerprints_their_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    validated = validate_task_definition_template(_d3_template(signer))
    assert validated["source_task_definition_arn"] == REV9_ARN
    assert validated["app_environment_names"] == sorted(
        set(BASE_ENV_VALUES) | set(D3_ENV)
    )
    assert validated["app_secret_names"] == sorted(BASE_SECRET_NAMES | set(D3_SECRETS))
    assert validated["base_environment_sha256"] == {
        name: _sha(value) for name, value in {**BASE_ENV_VALUES, **D3_ENV}.items()
    }


@pytest.mark.parametrize(
    ("field", "row", "reason"),
    [
        (
            "environment",
            {"name": "ASAAS_SANDBOX_API_URL", "value": "https://sandbox.example"},
            "task_definition_base_environment_names_mismatch",
        ),
        (
            "environment",
            {"name": "APPCONFIG_ENABLED", "value": "true"},
            "task_definition_base_environment_names_mismatch",
        ),
        (
            "environment",
            {"name": "ASAAS_API_KEY", "value": "inline-secret-never-allowed"},
            "task_definition_base_environment_names_mismatch",
        ),
        (
            "secrets",
            {"name": "ASAAS_SANDBOX_API_KEY", "valueFrom": SECRET_ARN},
            "task_definition_secret_names_mismatch",
        ),
        (
            "secrets",
            {"name": "ASAAS_WEBHOOK_URL", "valueFrom": SECRET_ARN},
            "task_definition_secret_names_mismatch",
        ),
    ],
)
def test_template_rejects_an_extra_unknown_name(
    monkeypatch: pytest.MonkeyPatch, field: str, row: dict[str, str], reason: str
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _d3_template(signer)
    _app_container(template["task_definition"])[field].append(row)
    with pytest.raises(EvidenceError, match=reason) as error:
        validate_task_definition_template(template)
    assert "inline-secret-never-allowed" not in str(error.value)


@pytest.mark.parametrize(
    ("field", "name", "reason"),
    [
        *(
            ("environment", name, "task_definition_base_environment_names_mismatch")
            for name in sorted(D3_ENV)
        ),
        (
            "environment",
            "QDRANT_HOST",
            "task_definition_base_environment_names_mismatch",
        ),
        *(
            ("secrets", name, "task_definition_secret_names_mismatch")
            for name in sorted(D3_SECRETS)
        ),
        ("secrets", "REDIS_URL", "task_definition_secret_names_mismatch"),
    ],
)
def test_template_rejects_a_missing_name(
    monkeypatch: pytest.MonkeyPatch, field: str, name: str, reason: str
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _drop_name(_d3_template(signer), field, name)
    with pytest.raises(EvidenceError, match=reason):
        validate_task_definition_template(template)


@pytest.mark.parametrize("missing", ["environment", "secrets"])
def test_template_rejects_half_of_the_d3_wiring(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _d3_template(signer)
    for name in D3_ENV if missing == "environment" else D3_SECRETS:
        _drop_name(template, missing, name)
    with pytest.raises(EvidenceError, match="task_definition_d3_wiring_partial"):
        validate_task_definition_template(template)


def test_revision_8_shape_keeps_its_names_but_its_tagged_image_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision: the revision 8 name sets still validate (D3 is all or nothing).

    The read-only D3 planner requires its rollback target (revision 8) to pass
    this validator, and a template can never drop the D3 wiring of a serving
    D3 revision because its environment fingerprints must equal the serving
    snapshot's. The real revision 8 still cannot use the protected path: its
    app image is a mutable tag in the source repository.
    """

    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _rev8_template(signer)
    validated = validate_task_definition_template(template)
    assert set(validated["base_environment_sha256"]) == set(BASE_ENV_VALUES)
    with pytest.raises(
        EvidenceError,
        match="^source_image_repository_mismatch:source_image_reference_not_digest_pinned$",
    ):
        build_replacement_task_definition(
            template,
            image_uri=IMAGE_URI,
            app_digest=DIGEST,
            pinned_database_value_from=PINNED_DATABASE_REFERENCE,
        )
    serving_rev8 = _d3_snapshot(image=REV8_TAGGED_IMAGE)
    _drop_d3_environment(serving_rev8)
    with pytest.raises(
        RolloutError,
        match="^task_definition_image_mismatch:source_image_reference_not_digest_pinned$",
    ):
        _validate_d3_snapshot(serving_rev8)


def test_a_revision_8_template_cannot_drop_the_wiring_of_a_serving_d3_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    rev8 = validate_task_definition_template(
        cast(dict[str, Any], _task_definition_template(signer))
    )
    with pytest.raises(RolloutError, match="^base_task_environment_changed$"):
        _validate_d3_snapshot(
            _d3_snapshot(), expected_base=rev8["base_environment_sha256"]
        )
    d3 = validate_task_definition_template(_d3_template(signer))
    serving_rev8 = _d3_snapshot()
    _drop_d3_environment(serving_rev8)
    with pytest.raises(RolloutError, match="^base_task_environment_changed$"):
        _validate_d3_snapshot(serving_rev8, expected_base=d3["base_environment_sha256"])


@pytest.mark.parametrize(
    ("reference", "origin"),
    [
        (SOURCE_IMAGE, "source"),
        (f"{IMAGE_URI}@{DIGEST}", "target"),
        (f"{IMAGE_URI}:active", "target"),
        (f"{IMAGE_URI}:active@{DIGEST}", "target"),
        # The Docker tag limit: one leading word character plus 127 more.
        (f"{IMAGE_URI}:" + "t" * 128, "target"),
    ],
)
def test_image_rule_accepts_the_target_or_a_digest_pinned_source(
    reference: str, origin: str
) -> None:
    assert rollout.classify_app_image(reference, target_repository=IMAGE_URI) == origin


@pytest.mark.parametrize(
    ("reference", "reason"),
    [
        (REV8_TAGGED_IMAGE, "source_image_reference_not_digest_pinned"),
        (
            f"{SOURCE_REPOSITORY}:bridge@{SOURCE_DIGEST}",
            "source_image_reference_not_digest_pinned",
        ),
        (SOURCE_REPOSITORY, "source_image_reference_not_digest_pinned"),
        (
            f"{SOURCE_REPOSITORY}@sha256:" + "A" * 64,
            "source_image_reference_not_digest_pinned",
        ),
        (
            f"{SOURCE_REPOSITORY}@sha256:" + "5" * 63,
            "source_image_reference_not_digest_pinned",
        ),
        (
            f"{SOURCE_REPOSITORY}@{SOURCE_DIGEST}\n",
            "source_image_reference_not_digest_pinned",
        ),
        (
            SOURCE_IMAGE.replace("388460142431", "111111111111"),
            "app_image_repository_not_allowed",
        ),
        (
            SOURCE_IMAGE.replace("us-east-1", "us-east-2"),
            "app_image_repository_not_allowed",
        ),
        (
            f"docker.io/library/neurostack-staging@{SOURCE_DIGEST}",
            "app_image_repository_not_allowed",
        ),
        (
            f"{SOURCE_REPOSITORY}-evil@{SOURCE_DIGEST}",
            "app_image_repository_not_allowed",
        ),
        (
            f"{SOURCE_REPOSITORY}/nested@{SOURCE_DIGEST}",
            "app_image_repository_not_allowed",
        ),
        (f"{IMAGE_URI}-evil@{DIGEST}", "app_image_repository_not_allowed"),
        (f"{IMAGE_URI}/nested@{DIGEST}", "app_image_repository_not_allowed"),
        (f"{REGISTRY}/neurostack-other@{DIGEST}", "app_image_repository_not_allowed"),
        (f"{IMAGE_URI}:", "app_image_reference_invalid"),
        (f"{IMAGE_URI}@sha256:abc", "app_image_reference_invalid"),
        (f"{IMAGE_URI}@{DIGEST}:active", "app_image_reference_invalid"),
        (f"{IMAGE_URI}@sha256:" + "A" * 64, "app_image_reference_invalid"),
        (f"{IMAGE_URI}:active@sha256:" + "A" * 64, "app_image_reference_invalid"),
        (f"{IMAGE_URI}:" + "t" * 129, "app_image_reference_invalid"),
        (None, "app_image_reference_invalid"),
        ("", "app_image_repository_not_allowed"),
    ],
)
def test_image_rule_fails_closed_on_anything_else(
    reference: object, reason: str
) -> None:
    with pytest.raises(RolloutError, match=f"^{reason}$"):
        rollout.classify_app_image(reference, target_repository=IMAGE_URI)


@pytest.mark.parametrize(
    "target",
    [
        SOURCE_REPOSITORY,
        "",
        None,
        IMAGE_URI.replace("388460142431", "111111111111"),
        IMAGE_URI.replace("us-east-1", "us-west-2"),
        IMAGE_URI.replace("us-east-1", "us-east-2"),
        f"{IMAGE_URI}@{DIGEST}",
        f"{IMAGE_URI}:active",
        # The ECR repository-name grammar (lowercase, single separators).
        f"{REGISTRY}/Neurostack/asaas-producer",
        f"{REGISTRY}/NEUROSTACK-PRODUCER",
        f"{SOURCE_REPOSITORY}/",
        f"{IMAGE_URI}/",
        f"{IMAGE_URI}.",
        f"{IMAGE_URI}-",
        f"{REGISTRY}/neurostack//asaas-producer",
        f"{REGISTRY}/a..b",
        f"{REGISTRY}/a.-b",
        f"{REGISTRY}/-neurostack",
        f"{REGISTRY}/.neurostack",
        f"{REGISTRY}/neurostack/_producer",
        f"{REGISTRY}/",
        f"{REGISTRY}//neurostack",
        f"{REGISTRY}/neurostack producer",
    ],
)
def test_image_rule_rejects_a_target_that_is_not_an_attestable_repository(
    target: object,
) -> None:
    with pytest.raises(RolloutError, match="^target_image_repository_invalid$"):
        rollout.classify_app_image(SOURCE_IMAGE, target_repository=target)


@pytest.mark.parametrize(
    "target",
    [
        IMAGE_URI,
        f"{REGISTRY}/neurostack-asaas-producer-release-"
        "producerimagerepository-jqtdjqst9o1j",
        f"{REGISTRY}/a.b_c-d/e",
        f"{REGISTRY}/a/b/c",
        f"{REGISTRY}/n7",
    ],
)
def test_image_rule_accepts_a_well_formed_target_repository(target: str) -> None:
    assert rollout.validate_target_image_repository(target) == target
    assert (
        rollout.classify_app_image(f"{target}@{DIGEST}", target_repository=target)
        == "target"
    )


def test_replacement_from_a_digest_pinned_source_image_attests_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    request = build_replacement_task_definition(
        _d3_template(signer),
        image_uri=IMAGE_URI,
        app_digest=DIGEST,
        pinned_database_value_from=PINNED_DATABASE_REFERENCE,
    )
    app = _app_container(request)
    assert app["image"] == f"{IMAGE_URI}@{DIGEST}"
    environment = {row["name"]: row["value"] for row in app["environment"]}
    assert set(environment) == set(BASE_ENV_VALUES) | set(D3_ENV) | set(
        _pinned_task_environment()
    )
    assert {name: environment[name] for name in D3_ENV} == D3_ENV
    secrets = {row["name"]: row["valueFrom"] for row in app["secrets"]}
    assert set(secrets) == BASE_SECRET_NAMES | set(D3_SECRETS)
    assert {name: secrets[name] for name in D3_SECRETS} == D3_SECRETS
    assert secrets["DATABASE_URL"] == PINNED_DATABASE_REFERENCE


@pytest.mark.parametrize(
    ("image", "image_uri", "reason"),
    [
        # Image from an unknown repository in the right account and region.
        (
            f"{REGISTRY}/neurostack-other@{SOURCE_DIGEST}",
            IMAGE_URI,
            "^source_image_repository_mismatch:app_image_repository_not_allowed$",
        ),
        # A mutable tag in the source repository.
        (
            f"{SOURCE_REPOSITORY}:bridge",
            IMAGE_URI,
            "^source_image_repository_mismatch:source_image_reference_not_digest_pinned$",
        ),
        # The source repository can never be the attested target.
        (SOURCE_IMAGE, SOURCE_REPOSITORY, "^app_image_uri_invalid$"),
        # A template captured from one target cannot be replaced into another.
        (
            f"{IMAGE_URI}@{DIGEST}",
            f"{IMAGE_URI}-other",
            "^source_image_repository_mismatch:app_image_repository_not_allowed$",
        ),
        # Prefix confusion: the old startswith() accepted this repository.
        (
            f"{IMAGE_URI}-evil@{DIGEST}",
            IMAGE_URI,
            "^source_image_repository_mismatch:app_image_repository_not_allowed$",
        ),
    ],
)
def test_replacement_rejects_unknown_repository_tag_and_target_mismatch(
    monkeypatch: pytest.MonkeyPatch, image: str, image_uri: str, reason: str
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _d3_template(signer, image=image)
    with pytest.raises(EvidenceError, match=reason):
        build_replacement_task_definition(
            template,
            image_uri=image_uri,
            app_digest=DIGEST,
            pinned_database_value_from=PINNED_DATABASE_REFERENCE,
        )


def test_serving_snapshot_may_run_the_digest_pinned_source_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = validate_task_definition_template(_d3_template(signer))
    checked = _validate_d3_snapshot(
        _d3_snapshot(), expected_base=template["base_environment_sha256"]
    )
    assert checked["app_image_digest"] == SOURCE_DIGEST
    assert checked["base_environment_sha256"] == template["base_environment_sha256"]
    assert set(checked["base_environment_sha256"]) == set(BASE_ENV_VALUES) | set(D3_ENV)


@pytest.mark.parametrize(
    ("phase", "image", "target", "reason"),
    [
        (
            "before",
            f"{SOURCE_REPOSITORY}@sha256:" + "6" * 64,
            IMAGE_URI,
            "^task_definition_image_mismatch$",
        ),
        (
            "before",
            f"{SOURCE_REPOSITORY}:asaas-gateway",
            IMAGE_URI,
            "^task_definition_image_mismatch:source_image_reference_not_digest_pinned$",
        ),
        (
            "before",
            f"{REGISTRY}/neurostack-other@{SOURCE_DIGEST}",
            IMAGE_URI,
            "^task_definition_image_mismatch:app_image_repository_not_allowed$",
        ),
        (
            "before",
            f"{SOURCE_REPOSITORY}-evil@{SOURCE_DIGEST}",
            IMAGE_URI,
            "^task_definition_image_mismatch:app_image_repository_not_allowed$",
        ),
        ("before", None, SOURCE_REPOSITORY, "^target_image_repository_invalid$"),
        ("before", None, "", "^target_image_repository_invalid$"),
        (
            "after",
            f"{SOURCE_REPOSITORY}@{DIGEST}",
            IMAGE_URI,
            "^task_definition_image_mismatch$",
        ),
        ("after", f"{IMAGE_URI}:active", IMAGE_URI, "^task_definition_image_mismatch$"),
        ("after", None, SOURCE_REPOSITORY, "^target_image_repository_invalid$"),
    ],
)
def test_snapshot_image_rule_fails_closed(
    phase: str, image: str | None, target: str, reason: str
) -> None:
    raw = _d3_snapshot(phase=phase, image=image)
    with pytest.raises(RolloutError, match=reason):
        _validate_d3_snapshot(raw, phase=phase, expected_image_uri=target)


def test_attested_snapshot_accepts_exactly_the_d3_names() -> None:
    checked = _validate_d3_snapshot(_d3_snapshot(phase="after"), phase="after")
    assert checked["app_image_digest"] == DIGEST
    assert checked["base_environment_sha256"] == {
        name: _sha(value) for name, value in {**BASE_ENV_VALUES, **D3_ENV}.items()
    }


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    "added,accepted",
    [
        (STAGED_FLAG_ENV, True),
        ({**STAGED_FLAG_ENV, **APPCONFIG_ENV}, True),
        ({"CREDIT_HOT_PATH_ENABLED": "false"}, False),
        (APPCONFIG_ENV, False),
        ({**STAGED_FLAG_ENV, "APPCONFIG_SOURCE": "sdk"}, False),
    ],
)
def test_snapshot_accepts_each_staged_set_whole_and_fingerprints_it(
    phase: str, added: dict[str, str], accepted: bool
) -> None:
    raw = _d3_snapshot(phase=phase)
    app = _app_container(raw["task_definition"])
    app["environment"] += [{"name": n, "value": v} for n, v in added.items()]
    if not accepted:
        with pytest.raises(RolloutError, match="^task_environment_name_set_mismatch$"):
            _validate_d3_snapshot(raw, phase=phase)
        return
    checked = _validate_d3_snapshot(raw, phase=phase)
    assert checked["base_environment_sha256"] == {
        name: _sha(value)
        for name, value in {**BASE_ENV_VALUES, **D3_ENV, **added}.items()
    }


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize(
    "change",
    [
        *(("drop", name) for name in sorted(D3_ENV)),
        ("drop", "HOST"),
        ("add", "ASAAS_SANDBOX_API_URL"),
        ("add", "APPCONFIG_ENABLED"),
        ("add", "ASAAS_API_KEY"),
    ],
)
def test_snapshot_rejects_a_missing_or_extra_environment_name(
    phase: str, change: tuple[str, str]
) -> None:
    raw = _d3_snapshot(phase=phase)
    app = _app_container(raw["task_definition"])
    action, name = change
    if action == "drop":
        app["environment"] = [row for row in app["environment"] if row["name"] != name]
    else:
        app["environment"].append({"name": name, "value": "x"})
    with pytest.raises(RolloutError, match="^task_environment_name_set_mismatch$"):
        _validate_d3_snapshot(raw, phase=phase)


@pytest.mark.parametrize("name", sorted(D3_ENV))
def test_d3_value_drift_from_the_template_is_detected_before_deployment(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = validate_task_definition_template(_d3_template(signer))
    drifted = _set_env(_d3_snapshot(), name, D3_ENV[name] + "-drifted")
    with pytest.raises(RolloutError, match="^base_task_environment_changed$"):
        _validate_d3_snapshot(
            drifted, expected_base=template["base_environment_sha256"]
        )


@pytest.mark.parametrize("name", sorted(D3_ENV))
def test_d3_values_are_part_of_the_runtime_fingerprint(name: str) -> None:
    definition = _d3_snapshot()["task_definition"]
    drifted = copy.deepcopy(definition)
    _set_env({"task_definition": drifted}, name, D3_ENV[name] + "-drifted")
    assert rollout._task_definition_runtime_fingerprint(
        definition
    ) != rollout._task_definition_runtime_fingerprint(drifted)
    # The trust pins and commercial gates the rollout adds stay outside it.
    with_pins = copy.deepcopy(definition)
    _app_container(with_pins)["environment"].append(
        {"name": "BILLING_ROLLOUT_STATE", "value": "disabled"}
    )
    assert rollout._task_definition_runtime_fingerprint(
        definition
    ) == rollout._task_definition_runtime_fingerprint(with_pins)


@pytest.mark.parametrize("name", sorted(D3_ENV))
def test_d3_value_drift_between_before_and_after_is_detected(name: str) -> None:
    before = _validate_d3_snapshot(_d3_snapshot())
    after = _validate_d3_snapshot(
        _set_env(_d3_snapshot(phase="after"), name, D3_ENV[name] + "-drifted"),
        phase="after",
    )
    with pytest.raises(
        RolloutError, match="readback_runtime_fingerprint_sha256_changed"
    ):
        compare_rollout_readbacks(before, after, expected_digest=DIGEST)
    # With equal runtime fingerprints the environment map still catches it.
    after["runtime_fingerprint_sha256"] = before["runtime_fingerprint_sha256"]
    with pytest.raises(RolloutError, match="readback_base_environment_sha256_changed"):
        compare_rollout_readbacks(before, after, expected_digest=DIGEST)


@pytest.mark.parametrize(
    "serving_database_reference", [SECRET_ARN, PINNED_DATABASE_REFERENCE]
)
@pytest.mark.parametrize("name", sorted(D3_SECRETS))
def test_d3_secret_reference_swap_between_before_and_after_is_detected(
    monkeypatch: pytest.MonkeyPatch, name: str, serving_database_reference: str
) -> None:
    """The snapshot has no secret-name set: the runtime fingerprint is the guard.

    A template that points one D3 secret at a different secret passes every
    pre-deployment check; the before/after comparison after the service update
    is what rejects it (and the executor then rolls back).
    """

    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    honest = _d3_template(signer)
    serving = copy.deepcopy(honest["task_definition"])
    for row in _app_container(serving)["secrets"]:
        if row["name"] == "DATABASE_URL":
            row["valueFrom"] = serving_database_reference
    forged = copy.deepcopy(honest)
    for row in _app_container(forged["task_definition"])["secrets"]:
        if row["name"] == name:
            row["valueFrom"] = _D3_SECRET_PREFIX + "staging/other-secret-ZzZ999"
    validated = validate_task_definition_template(forged)
    common: dict[str, Any] = {
        "expected_image_uri": IMAGE_URI,
        "expected_task_role_arn": TASK_ROLE,
        "expected_execution_role_arn": EXECUTION_ROLE,
        "expected_secret_arn": SECRET_ARN,
        "expected_secret_json_key": "",
        "expected_secret_version_id": VERSION_ID,
        "expected_task_environment": _pinned_task_environment(),
        "external_readiness_verified": True,
    }
    before = validate_deployment_snapshot(
        _snapshot_of(serving, arn=REV9_ARN, running_digest=SOURCE_DIGEST),
        expected_digest="current",
        expected_base_environment_sha256=validated["base_environment_sha256"],
        phase="before",
        **common,
    )
    request = build_replacement_task_definition(
        forged,
        image_uri=IMAGE_URI,
        app_digest=DIGEST,
        pinned_database_value_from=PINNED_DATABASE_REFERENCE,
    )
    after = validate_deployment_snapshot(
        _snapshot_of(request, arn=REV10_ARN, running_digest=DIGEST),
        expected_digest=DIGEST,
        expected_base_environment_sha256=before["base_environment_sha256"],
        phase="after",
        **common,
    )
    assert before["base_environment_sha256"] == after["base_environment_sha256"]
    with pytest.raises(
        RolloutError, match="^readback_runtime_fingerprint_sha256_changed$"
    ):
        compare_rollout_readbacks(before, after, expected_digest=DIGEST)


@pytest.mark.parametrize("name", sorted(D3_SECRETS))
def test_d3_secret_references_are_part_of_the_runtime_fingerprint(name: str) -> None:
    definition = _d3_snapshot()["task_definition"]
    swapped = copy.deepcopy(definition)
    for row in _app_container(swapped)["secrets"]:
        if row["name"] == name:
            row["valueFrom"] = _D3_SECRET_PREFIX + "staging/other-secret-ZzZ999"
    assert rollout._task_definition_runtime_fingerprint(
        definition
    ) != rollout._task_definition_runtime_fingerprint(swapped)


def test_sanitized_readback_carries_d3_value_fingerprints_without_values() -> None:
    normalized = _d3_snapshot()
    raw = {
        "service_response": {"services": [normalized["service"]], "failures": []},
        "tasks_response": {
            "tasks": [
                {
                    **normalized["tasks"][0],
                    "attachments": [
                        {
                            "details": [
                                {"name": "privateIPv4Address", "value": "10.0.1.7"}
                            ]
                        }
                    ],
                },
            ],
            "failures": [],
        },
        "task_definition_response": {"taskDefinition": normalized["task_definition"]},
        "ecr_response": normalized["ecr"],
        "secret_description": normalized["secret_description"],
        "secret_versions": normalized["secret_versions"],
    }
    projected = sanitize_aws_readbacks(raw)
    encoded = json.dumps(projected, sort_keys=True)
    for value in D3_ENV.values():
        if value != "false":
            assert value not in encoded
    rows = {
        row["name"]: row
        for row in _app_container(projected["task_definition"])["environment"]
    }
    for name, value in D3_ENV.items():
        assert rows[name] == {
            "name": name,
            "value": "<redacted>",
            "value_sha256": _sha(value),
        }
    assert projected["task_definition"][
        "runtime_fingerprint_sha256"
    ] == rollout._task_definition_runtime_fingerprint(normalized["task_definition"])
    checked = _validate_d3_snapshot(
        projected,
        expected_base={
            name: _sha(value) for name, value in {**BASE_ENV_VALUES, **D3_ENV}.items()
        },
    )
    assert checked["runtime_fingerprint_sha256"] == (
        projected["task_definition"]["runtime_fingerprint_sha256"]
    )


@pytest.mark.parametrize(
    "serving_database_reference",
    # The plain ARN is what revision 9 serves and what the template must carry;
    # the pinned form is a serving revision a previous protected deploy wrote.
    [SECRET_ARN, PINNED_DATABASE_REFERENCE],
)
def test_path_a_round_trip_from_a_serving_d3_revision_to_the_attested_image(
    monkeypatch: pytest.MonkeyPatch, serving_database_reference: str
) -> None:
    """Template from revision 9 -> before -> replacement -> after -> compare."""

    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    template = _d3_template(signer)
    validated = validate_task_definition_template(template)
    serving = copy.deepcopy(template["task_definition"])
    for row in _app_container(serving)["secrets"]:
        if row["name"] == "DATABASE_URL":
            row["valueFrom"] = serving_database_reference
    before = validate_deployment_snapshot(
        _snapshot_of(serving, arn=REV9_ARN, running_digest=SOURCE_DIGEST),
        expected_digest="current",
        expected_image_uri=IMAGE_URI,
        expected_task_role_arn=TASK_ROLE,
        expected_execution_role_arn=EXECUTION_ROLE,
        expected_secret_arn=SECRET_ARN,
        expected_secret_json_key="",
        expected_secret_version_id=VERSION_ID,
        expected_task_environment=_pinned_task_environment(),
        expected_base_environment_sha256=validated["base_environment_sha256"],
        phase="before",
        external_readiness_verified=True,
    )
    request = build_replacement_task_definition(
        template,
        image_uri=IMAGE_URI,
        app_digest=DIGEST,
        pinned_database_value_from=PINNED_DATABASE_REFERENCE,
    )
    after = validate_deployment_snapshot(
        _snapshot_of(request, arn=REV10_ARN, running_digest=DIGEST),
        expected_digest=DIGEST,
        expected_image_uri=IMAGE_URI,
        expected_task_role_arn=TASK_ROLE,
        expected_execution_role_arn=EXECUTION_ROLE,
        expected_secret_arn=SECRET_ARN,
        expected_secret_json_key="",
        expected_secret_version_id=VERSION_ID,
        expected_task_environment=_pinned_task_environment(),
        expected_base_environment_sha256=before["base_environment_sha256"],
        phase="after",
        external_readiness_verified=True,
    )
    result = compare_rollout_readbacks(before, after, expected_digest=DIGEST)
    assert result["previous_image_digest"] == SOURCE_DIGEST
    assert result["active_image_digest"] == DIGEST
    assert result["base_environment_sha256"] == validated["base_environment_sha256"]
    assert set(result["base_environment_sha256"]) == set(BASE_ENV_VALUES) | set(D3_ENV)


@pytest.mark.parametrize("shape", ["revision_8", "revision_9"])
def test_protected_deploy_round_trips_a_plain_database_url_serving_revision(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """F1 residual 5: the template and the serving revision carry the plain ARN.

    The replacement pins DATABASE_URL to ``<ARN>:::<VersionId>``. Both forms
    must normalize to the same ARN and JSON key, or the first protected
    deploy fails ``readback_runtime_fingerprint_sha256_changed`` after
    ``update-service`` and rolls back.
    """

    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    if shape == "revision_8":
        template = cast(dict[str, Any], _task_definition_template(signer))
        _app_container(template["task_definition"])["image"] = SOURCE_IMAGE
    else:
        template = _d3_template(signer)
    validated = validate_task_definition_template(template)
    serving = copy.deepcopy(template["task_definition"])
    assert {
        row["name"]: row["valueFrom"] for row in _app_container(serving)["secrets"]
    }["DATABASE_URL"] == SECRET_ARN
    serving_arn = template["source_task_definition_arn"]
    kwargs: dict[str, Any] = {
        "expected_image_uri": IMAGE_URI,
        "expected_task_role_arn": TASK_ROLE,
        "expected_execution_role_arn": EXECUTION_ROLE,
        "expected_secret_arn": SECRET_ARN,
        "expected_secret_json_key": "",
        "expected_secret_version_id": VERSION_ID,
        "expected_task_environment": _pinned_task_environment(),
        "external_readiness_verified": True,
    }
    before = validate_deployment_snapshot(
        _snapshot_of(serving, arn=serving_arn, running_digest=SOURCE_DIGEST),
        expected_digest="current",
        expected_base_environment_sha256=validated["base_environment_sha256"],
        phase="before",
        **kwargs,
    )
    request = build_replacement_task_definition(
        template,
        image_uri=IMAGE_URI,
        app_digest=DIGEST,
        pinned_database_value_from=PINNED_DATABASE_REFERENCE,
    )
    assert {
        row["name"]: row["valueFrom"] for row in _app_container(request)["secrets"]
    }["DATABASE_URL"] == PINNED_DATABASE_REFERENCE
    after = validate_deployment_snapshot(
        _snapshot_of(
            request, arn=serving_arn.rsplit(":", 1)[0] + ":99", running_digest=DIGEST
        ),
        expected_digest=DIGEST,
        expected_base_environment_sha256=before["base_environment_sha256"],
        phase="after",
        **kwargs,
    )
    result = compare_rollout_readbacks(before, after, expected_digest=DIGEST)
    assert result["active_image_digest"] == DIGEST


_OTHER_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:388460142431:secret:redis-123456"
_OTHER_DB_ARN = SECRET_ARN.replace("neurostack-db", "neurostack-other")


def _database_url_definition(
    reference: str | None, *, other_secrets: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """A minimal definition whose fingerprint pins the secret normalization."""

    secrets = (
        [
            {"name": "REDIS_URL", "valueFrom": _OTHER_SECRET_ARN},
            {"name": "LLM_API_KEY", "valueFrom": f"{_OTHER_SECRET_ARN}:api_key::"},
            {
                "name": "SECRET_KEY",
                "valueFrom": f"{_OTHER_SECRET_ARN}:key::{VERSION_ID}",
            },
        ]
        if other_secrets is None
        else other_secrets
    )
    if reference is not None:
        secrets = [{"name": "DATABASE_URL", "valueFrom": reference}, *secrets]
    return {
        "family": "neurostack-staging-app",
        "cpu": "1024",
        "containerDefinitions": [
            {
                "name": "app",
                "image": "ignored",
                "environment": [{"name": "ENVIRONMENT", "value": "staging"}],
                "secrets": secrets,
            },
            {"name": "otel", "image": "public.ecr.aws/otel/collector:1", "secrets": []},
        ],
    }


def _fingerprint(reference: str | None, **kwargs: Any) -> str:
    return rollout._task_definition_runtime_fingerprint(
        _database_url_definition(reference, **kwargs)
    )


# Computed on origin/main 1fff19e7, before the plain-ARN fix: the fix must not
# move any 10-part reference or any other secret.
_GOLDEN_EMPTY_KEY = "1e7bbc3f7548e0fa3c958d76e4ad1db29817602619f073b9d05c8bacf5e1753a"
_GOLDEN_JSON_KEY = "6b4945c2faa05dd3da635c4477185688688c5eeb896e3929de8c3b81cacdef6e"
_GOLDEN_OTHER_ARN = "71313d12f3cc0c89a7cfdfd791bfc77ba7afaf8a4438e9c027885c45898b5d8a"
_GOLDEN_NO_DATABASE_URL = (
    "c1d3652580361faadbfc8da7042b99a284c5aef9bad5282f2ab8f941cf237cd3"
)


@pytest.mark.parametrize(
    ("reference", "golden"),
    [
        (f"{SECRET_ARN}:::{VERSION_ID}", _GOLDEN_EMPTY_KEY),
        (f"{SECRET_ARN}:::", _GOLDEN_EMPTY_KEY),
        (f"{SECRET_ARN}:database_url::{VERSION_ID}", _GOLDEN_JSON_KEY),
        (f"{SECRET_ARN}:database_url:AWSCURRENT:", _GOLDEN_JSON_KEY),
        (f"{_OTHER_DB_ARN}:::{VERSION_ID}", _GOLDEN_OTHER_ARN),
        (None, _GOLDEN_NO_DATABASE_URL),
    ],
)
def test_ten_part_database_url_and_other_secret_fingerprints_are_unchanged(
    reference: str | None, golden: str
) -> None:
    assert _fingerprint(reference) == golden


@pytest.mark.parametrize(
    "reference",
    [
        SECRET_ARN,
        f"{SECRET_ARN}:::",
        f"{SECRET_ARN}:::{VERSION_ID}",
        f"{SECRET_ARN}:::87654321-4321-8765-4321-876543218765",
        f"{SECRET_ARN}::AWSCURRENT:",
        f"{SECRET_ARN}::AWSPREVIOUS:",
    ],
)
def test_database_url_fingerprint_binds_the_arn_and_json_key_not_the_version(
    reference: str,
) -> None:
    """A plain ARN is ``<ARN>`` with an empty JSON key and the default version.

    The version is the one change the protected deploy makes on purpose
    (``build_replacement_task_definition`` pins it); the snapshot validator
    checks it separately against ``expected_secret_version_id``.
    """

    assert _fingerprint(reference) == _GOLDEN_EMPTY_KEY


@pytest.mark.parametrize(
    "reference",
    [
        f"{_OTHER_DB_ARN}:::{VERSION_ID}",
        _OTHER_DB_ARN,
        f"{SECRET_ARN}:database_url::{VERSION_ID}",
        f"{SECRET_ARN}:password::{VERSION_ID}",
        SECRET_ARN.replace("us-east-1", "us-east-2"),
        SECRET_ARN.replace("388460142431", "111111111111") + f":::{VERSION_ID}",
        SECRET_ARN.replace("neurostack-db-a1b2c3", "neurostack-db-zzzzzz"),
    ],
)
def test_database_url_fingerprint_still_changes_with_the_secret_or_json_key(
    reference: str,
) -> None:
    assert _fingerprint(reference) != _fingerprint(SECRET_ARN)
    assert _fingerprint(reference) != _GOLDEN_EMPTY_KEY


def test_database_url_json_keys_stay_distinct_from_each_other() -> None:
    assert _fingerprint(f"{SECRET_ARN}:database_url::{VERSION_ID}") != _fingerprint(
        f"{SECRET_ARN}:password::{VERSION_ID}"
    )


@pytest.mark.parametrize(
    "reference",
    [
        f"{SECRET_ARN}:database_url",
        f"{SECRET_ARN}:database_url:",
        f"{SECRET_ARN}:database_url::{VERSION_ID}:extra",
        "postgresql://user:password@db.example/private",
    ],
)
def test_database_url_fingerprint_rejects_other_reference_shapes(
    reference: str,
) -> None:
    with pytest.raises(RolloutError, match="^database_url_reference_invalid$"):
        _fingerprint(reference)


@pytest.mark.parametrize(
    "variant",
    [
        f"{_OTHER_SECRET_ARN}:::{VERSION_ID}",
        f"{_OTHER_SECRET_ARN}:",
        f"{_OTHER_SECRET_ARN}::AWSCURRENT:",
    ],
)
def test_other_secrets_still_hash_the_whole_reference(variant: str) -> None:
    """Only DATABASE_URL is normalized; any other secret's form is bound as is."""

    changed = [
        {"name": "REDIS_URL", "valueFrom": variant},
        {"name": "LLM_API_KEY", "valueFrom": f"{_OTHER_SECRET_ARN}:api_key::"},
        {"name": "SECRET_KEY", "valueFrom": f"{_OTHER_SECRET_ARN}:key::{VERSION_ID}"},
    ]
    assert _fingerprint(None, other_secrets=changed) != _GOLDEN_NO_DATABASE_URL


def test_either_reviewed_worker_flag_passes_and_its_value_is_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E7-B0: the outbox worker flag may be 'false' or 'true' (a reviewed
    parameter of the D3 planner). The protected path accepts either value and
    binds it like every D3 value: a template captured from a 'false' revision
    refuses a serving 'true' revision, so after the flip the template must be
    regenerated from the serving revision before a protected deploy."""

    flag = "BILLING_FACT_WORKER_ENABLED"
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    templates = {}
    for value in ("false", "true"):
        template = _set_env(_d3_template(signer), flag, value)
        templates[value] = validate_task_definition_template(template)
        assert templates[value]["base_environment_sha256"][flag] == _sha(value)
    serving_true = _set_env(_d3_snapshot(), flag, "true")
    _validate_d3_snapshot(
        copy.deepcopy(serving_true),
        expected_base=templates["true"]["base_environment_sha256"],
    )
    with pytest.raises(RolloutError, match="^base_task_environment_changed$"):
        _validate_d3_snapshot(
            serving_true, expected_base=templates["false"]["base_environment_sha256"]
        )
    before = _validate_d3_snapshot(_set_env(_d3_snapshot(), flag, "true"))
    after = _validate_d3_snapshot(
        _set_env(_d3_snapshot(phase="after"), flag, "true"), phase="after"
    )
    compare_rollout_readbacks(before, after, expected_digest=DIGEST)
    flipped = _validate_d3_snapshot(
        _set_env(_d3_snapshot(phase="after"), flag, "false"), phase="after"
    )
    with pytest.raises(
        RolloutError, match="readback_runtime_fingerprint_sha256_changed"
    ):
        compare_rollout_readbacks(before, flipped, expected_digest=DIGEST)
