from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/sign_asaas_build_attestation.py"
)
_SPEC = importlib.util.spec_from_file_location("asaas_attestation_cli_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
cli = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cli
_SPEC.loader.exec_module(cli)


class P256TestSigner:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())

    def sign_digest(self, key_arn: str, digest: bytes) -> bytes:
        assert (
            key_arn
            == "arn:aws:kms:us-east-1:388460142431:key/01234567-89ab-cdef-0123-456789abcdef"
        )
        return self.key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    @property
    def public_key_pem(self) -> str:
        return (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )


def _environment(public_key_pem: str) -> dict[str, str]:
    repository = "testorg/neurostack-api"
    owner_id = "87654321"
    repository_id = "12345678"
    workflow_path = ".github/workflows/asaas-build-attestation.yml"
    workflow_ref = "refs/heads/main"
    oidc_subject = (
        f"repo:testorg@{owner_id}/neurostack-api@{repository_id}:"
        f"environment:asaas-build:ref:{workflow_ref}:workflow_ref:"
        f"{repository}/{workflow_path}@{workflow_ref}"
    )
    return {
        "BILLING_OPERATION_ID": str(uuid4()),
        "BILLING_PRODUCER_BUILD_ISSUER": "github-actions:test",
        "BILLING_PRODUCER_BUILD_KMS_KEY_ARN": "arn:aws:kms:us-east-1:388460142431:key/01234567-89ab-cdef-0123-456789abcdef",
        "BILLING_PRODUCER_BUILD_AUDIENCE": "test:asaas-producer",
        "BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM": public_key_pem,
        "BILLING_PRODUCER_BUILD_ROLE_ARN": "arn:aws:iam::388460142431:role/test-build-attestor",
        "BILLING_PRODUCER_BUILD_REPOSITORY_ID": repository_id,
        "BILLING_PRODUCER_BUILD_REPOSITORY_NAME": repository,
        "BILLING_PRODUCER_BUILD_WORKFLOW_PATH": workflow_path,
        "BILLING_PRODUCER_BUILD_WORKFLOW_REF": workflow_ref,
        "BILLING_PRODUCER_BUILD_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "BILLING_PRODUCER_BUILD_OIDC_SUBJECT": oidc_subject,
        "BILLING_PRODUCER_ECR_REPOSITORY_URI": "388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack/api",
        "GITHUB_REPOSITORY_ID": repository_id,
        "GITHUB_REPOSITORY_OWNER_ID": owner_id,
        "GITHUB_REPOSITORY": repository,
        "GITHUB_REF": workflow_ref,
        "GITHUB_WORKFLOW_REF": f"{repository}/{workflow_path}@{workflow_ref}",
        "GITHUB_RUN_ID": "7123456789",
        "GITHUB_SHA": "1" * 40,
        "GIT_COMMIT_OBJECT_SHA256": "5" * 64,
        "GIT_TREE_SHA": "2" * 64,
        "BUNDLE_DIGEST": "3" * 64,
        "APP_IMAGE_DIGEST": "sha256:" + "4" * 64,
    }


def test_missing_trust_pins_fail_before_any_aws_sdk_import(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", [str(_SCRIPT), "--output", "/tmp/attestation.json"]
    )
    monkeypatch.setattr(cli.os, "environ", {})
    monkeypatch.setitem(sys.modules, "boto3", None)

    assert cli.main() == 2
    assert "administrator trust pins are incomplete" in capsys.readouterr().err


def test_pin_preflight_is_local_and_rejects_oidc_mismatch(monkeypatch):
    signer = P256TestSigner()
    environment = _environment(signer.public_key_pem)
    monkeypatch.setattr(cli.os, "environ", environment)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--validate-pins"])
    monkeypatch.setitem(sys.modules, "boto3", None)

    assert cli.main() == 0

    environment["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"] = (
        "repo:other/repo:environment:wrong"
    )
    with pytest.raises(cli.AttestationCliError, match="OIDC subject"):
        cli._validate_administrator_pins(environment)


def test_oidc_subject_binds_immutable_repository_owner_environment_ref_and_workflow():
    signer = P256TestSigner()
    environment = _environment(signer.public_key_pem)

    pins = cli._validate_administrator_pins(environment)
    assert pins["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"] == (
        "repo:testorg@87654321/neurostack-api@12345678:"
        "environment:asaas-build:ref:refs/heads/main:workflow_ref:"
        "testorg/neurostack-api/.github/workflows/"
        "asaas-build-attestation.yml@refs/heads/main"
    )

    environment["GITHUB_REPOSITORY_OWNER_ID"] = "87654322"
    with pytest.raises(cli.AttestationCliError, match="OIDC subject"):
        cli._validate_administrator_pins(environment)


def test_legacy_environment_only_oidc_subject_is_rejected():
    signer = P256TestSigner()
    environment = _environment(signer.public_key_pem)
    environment["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"] = (
        "repo:testorg/neurostack-api:environment:billing-producer-build-attestation"
    )

    with pytest.raises(cli.AttestationCliError, match="OIDC subject"):
        cli._validate_administrator_pins(environment)


def test_local_p256_signer_emits_pinned_attestation_envelope():
    signer = P256TestSigner()
    pins = cli._validate_context(_environment(signer.public_key_pem))
    module = cli._load_envelope_module()
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)

    envelope = cli._create_envelope(pins, signer=signer, now=now)
    entry = module.TrustEntry(
        issuer=pins["BILLING_PRODUCER_BUILD_ISSUER"],
        kid=pins["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
        environment=module.ENVIRONMENT,
        audience=pins["BILLING_PRODUCER_BUILD_AUDIENCE"],
        type="billing_build_attestation.v1",
        purpose=module.PURPOSE_BY_TYPE["billing_build_attestation.v1"],
        schema_version=module.SCHEMA_VERSION,
        algorithm=module.ALGORITHM,
        public_key_pem=pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"],
        repository_id=pins["GITHUB_REPOSITORY_ID"],
        repository_name=pins["GITHUB_REPOSITORY"],
        workflow_path=pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"],
        workflow_ref=pins["GITHUB_REF"],
        oidc_issuer=pins["BILLING_PRODUCER_BUILD_OIDC_ISSUER"],
        oidc_subject=pins["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"],
    )
    verified = module.verify_envelope(
        envelope,
        module.TrustRegistry([entry]),
        expected_type="billing_build_attestation.v1",
    )
    module.validate_attestation_trust(verified, envelope)

    assert envelope["payload"]["workflow_id"] == (
        "github-actions:12345678:.github/workflows/asaas-build-attestation.yml"
    )
    assert envelope["payload"]["workflow_run_id"] == 7123456789
