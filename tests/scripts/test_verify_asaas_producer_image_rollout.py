from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from scripts.asaas_producer_rollout_evidence import (
    ENCRYPTED_READBACK_MARKER,
    EvidenceError,
    _validate_database_task_ip_binding,
    _task_readiness_check,
    build_replacement_task_definition,
    decrypt_operator_snapshot,
    encrypt_operator_snapshot,
    generate_post_readback_key,
    parse_post_readback_comment,
    validate_operator_readback,
    validate_before_after,
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

    assert validate_database_readiness_cli(
        ["--input", str(input_path), "--output", str(output_path)]
    ) == 0
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
            {"task_arn": f"task-{task_suffix}-1", "created_at": "2026-10-16T10:00:00Z", "started_at": "2026-10-16T10:00:05Z"},
            {"task_arn": f"task-{task_suffix}-2", "created_at": "2026-10-16T10:00:01Z", "started_at": "2026-10-16T10:00:06Z"},
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


def test_pre_oidc_cohort_allows_changed_tasks_only_with_fresh_readiness_contract() -> None:
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
