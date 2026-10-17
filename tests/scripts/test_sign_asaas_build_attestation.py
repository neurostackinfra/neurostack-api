from __future__ import annotations

import base64
import hashlib
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
    environment = {
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
        "ASAAS_BUILD_TRUST_MODE": "normal",
        "ASAAS_PREFLIGHT_BUILD_TRUST_MODE": "normal",
        "GITHUB_SHA": "1" * 40,
        "GIT_COMMIT_OBJECT_SHA256": "5" * 64,
        "GIT_TREE_SHA": "2" * 64,
        "BUNDLE_DIGEST": "3" * 64,
        "APP_IMAGE_DIGEST": "sha256:" + "4" * 64,
        "PIN_BACKEND_BASE_URL": "https://staging-api.example.test",
    }
    pins = cli._validate_administrator_pins(environment)
    readback = _backend_readback(pins)
    trust_environment = cli._backend_trust_environment(pins, environment)
    _validated, digest = cli._PREFLIGHT.validate_backend_trust_readback(
        trust_environment, readback
    )
    environment["ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256"] = digest
    return environment


def _backend_readback(pins: dict[str, str]) -> dict[str, str]:
    key = serialization.load_pem_public_key(
        pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"].encode("ascii")
    )
    fingerprint = hashlib.sha256(
        key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()
    return {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": "v1",
        "environment": "staging",
        "type": "billing_build_attestation.v1",
        "purpose": "billing-build-attestation.v1",
        "algorithm": "ECDSA_SHA_256",
        "issuer": pins["BILLING_PRODUCER_BUILD_ISSUER"],
        "kid": pins["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
        "audience": pins["BILLING_PRODUCER_BUILD_AUDIENCE"],
        "public_key_fingerprint_sha256": fingerprint,
        "repository_id": pins["BILLING_PRODUCER_BUILD_REPOSITORY_ID"],
        "repository_name": pins["BILLING_PRODUCER_BUILD_REPOSITORY_NAME"],
        "workflow_path": pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"],
        "workflow_ref": pins["BILLING_PRODUCER_BUILD_WORKFLOW_REF"],
        "oidc_issuer": pins["BILLING_PRODUCER_BUILD_OIDC_ISSUER"],
        "oidc_subject": pins["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"],
        "read_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def _bootstrap_public_key_pem() -> str:
    der = base64.b64decode(
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAECw5IaokbYv1CrOzYU0uaUMuj0DcROgxu9/eG"
        "bCLPmV8zztDfmrhjdq7HNJZ3/CGivJ6OQGqkVSBKEwqGycfwkw==",
        validate=True,
    )
    encoded = base64.b64encode(der).decode("ascii")
    lines = [encoded[index : index + 64] for index in range(0, len(encoded), 64)]
    return (
        "-----BEGIN PUBLIC KEY-----\n"
        + "\n".join(lines)
        + "\n-----END PUBLIC KEY-----\n"
    )


def _bootstrap_environment() -> dict[str, str]:
    repo = "neurostackinfra/neurostack-api"
    workflow_path = ".github/workflows/asaas-build-attestation.yml"
    workflow_ref = "refs/heads/main"
    oidc_subject = (
        "repo:neurostackinfra@340698182/neurostack-api@1152335815:"
        "environment:asaas-build:ref:refs/heads/main:workflow_ref:"
        f"{repo}/{workflow_path}@{workflow_ref}"
    )
    return {
        "BILLING_OPERATION_ID": str(uuid4()),
        "BILLING_PRODUCER_BUILD_ISSUER": "github-actions:neurostack-build",
        "BILLING_PRODUCER_BUILD_KMS_KEY_ARN": cli._PREFLIGHT.BOOTSTRAP_KMS_KEY_ARN,
        "BILLING_PRODUCER_BUILD_AUDIENCE": cli._PREFLIGHT.BOOTSTRAP_BUILD_AUDIENCE,
        "BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM": _bootstrap_public_key_pem(),
        "BILLING_PRODUCER_BUILD_ROLE_ARN": cli._PREFLIGHT.BOOTSTRAP_BUILD_ROLE_ARN,
        "BILLING_PRODUCER_BUILD_REPOSITORY_ID": "1152335815",
        "BILLING_PRODUCER_BUILD_REPOSITORY_NAME": repo,
        "BILLING_PRODUCER_BUILD_WORKFLOW_PATH": workflow_path,
        "BILLING_PRODUCER_BUILD_WORKFLOW_REF": workflow_ref,
        "BILLING_PRODUCER_BUILD_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "BILLING_PRODUCER_BUILD_OIDC_SUBJECT": oidc_subject,
        "BILLING_PRODUCER_ECR_REPOSITORY_URI": cli._PREFLIGHT.BOOTSTRAP_ECR_URI,
        "GITHUB_REPOSITORY_ID": "1152335815",
        "GITHUB_REPOSITORY_OWNER_ID": "340698182",
        "GITHUB_REPOSITORY": repo,
        "GITHUB_REF": workflow_ref,
        "GITHUB_WORKFLOW_REF": f"{repo}/{workflow_path}@{workflow_ref}",
        "GITHUB_RUN_ID": "7123456789",
        "GITHUB_RUN_ATTEMPT": "1",
        "PIN_BACKEND_BASE_URL": "https://staging.api.neurostack.global",
        "ASAAS_BUILD_TRUST_MODE": "bootstrap",
        "ASAAS_PREFLIGHT_BUILD_TRUST_MODE": "bootstrap",
        "ASAAS_PREFLIGHT_BACKEND_TRUST_PROBE_SHA256": "a" * 64,
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

    def fetch_backend_trust(trust_environment, *, expected_digest=None):
        pins = cli._validate_administrator_pins(environment)
        readback = _backend_readback(pins)
        return cli._PREFLIGHT.validate_backend_trust_readback(
            trust_environment, readback, expected_digest=expected_digest
        )

    monkeypatch.setattr(
        cli._PREFLIGHT, "fetch_backend_trust_readback", fetch_backend_trust
    )

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

    readback = _backend_readback(pins)
    entry = cli._trust_entry_from_backend_readback(pins, readback)
    envelope = cli._create_envelope(pins, signer=signer, trust_entry=entry, now=now)
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


def test_signer_trust_entry_comes_from_backend_readback_and_uses_pinned_public_key():
    signer = P256TestSigner()
    environment = _environment(signer.public_key_pem)
    pins = cli._validate_context(environment)
    readback = _backend_readback(pins)
    entry = cli._trust_entry_from_backend_readback(pins, readback)

    assert entry.issuer == readback["issuer"]
    assert entry.kid == readback["kid"]
    assert entry.audience == readback["audience"]
    assert entry.repository_id == readback["repository_id"]
    assert entry.public_key_pem == pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"]

    readback["issuer"] = "different-backend-issuer"
    with pytest.raises(cli._PREFLIGHT.GateError, match="backend_trust_pin_mismatch"):
        cli._PREFLIGHT.validate_backend_trust_readback(
            cli._backend_trust_environment(pins, environment), readback
        )


def test_bootstrap_signer_uses_committed_fingerprint_checked_public_key_pins(
    monkeypatch,
):
    environment = _bootstrap_environment()
    pins = cli._validate_administrator_pins(environment)
    observed = []

    def probe(trust_environment, *, expected_digest=None):
        observed.append((trust_environment, expected_digest))
        return expected_digest

    monkeypatch.setattr(
        cli._PREFLIGHT, "probe_backend_trust_readback_unavailable", probe
    )
    entry, digest = cli._active_backend_trust(pins, environment)

    assert digest == "a" * 64
    assert entry.issuer == pins["BILLING_PRODUCER_BUILD_ISSUER"]
    assert entry.kid == cli._PREFLIGHT.BOOTSTRAP_KMS_KEY_ARN
    assert entry.public_key_pem == pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"]
    assert entry.repository_id == "1152335815"
    assert entry.workflow_ref == "refs/heads/main"
    assert len(observed) == 1
    assert observed[0][1] == "a" * 64

    wrong_issuer_environment = dict(environment)
    wrong_issuer_environment["BILLING_PRODUCER_BUILD_ISSUER"] = "uncommitted-issuer"
    wrong_issuer_pins = cli._validate_administrator_pins(wrong_issuer_environment)
    with pytest.raises(cli._PREFLIGHT.GateError, match="bootstrap_trust_pin_mismatch"):
        cli._active_backend_trust(wrong_issuer_pins, wrong_issuer_environment)

    environment["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"] = (
        P256TestSigner().public_key_pem
    )
    pins = cli._validate_administrator_pins(environment)
    with pytest.raises(
        cli._PREFLIGHT.GateError, match="bootstrap_public_key_fingerprint_mismatch"
    ):
        cli._active_backend_trust(pins, environment)


def test_normal_signer_never_falls_back_to_pins_when_backend_is_unavailable(
    monkeypatch,
):
    signer = P256TestSigner()
    environment = _environment(signer.public_key_pem)
    environment["ASAAS_PREFLIGHT_BUILD_TRUST_MODE"] = "normal"
    pins = cli._validate_administrator_pins(environment)

    def unavailable(*_args, **_kwargs):
        raise cli._PREFLIGHT.GateError("backend_trust_readback_unavailable")

    monkeypatch.setattr(cli._PREFLIGHT, "fetch_backend_trust_readback", unavailable)
    with pytest.raises(
        cli._PREFLIGHT.GateError, match="backend_trust_readback_unavailable"
    ):
        cli._active_backend_trust(pins, environment)
