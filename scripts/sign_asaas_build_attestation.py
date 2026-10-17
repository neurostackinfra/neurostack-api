#!/usr/bin/env python3
"""Create one canonical CI build attestation after administrator pins validate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

_PREFLIGHT_PATH = Path(__file__).resolve().with_name("asaas_build_release_preflight.py")
_PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    "asaas_build_backend_trust_preflight", _PREFLIGHT_PATH
)
if _PREFLIGHT_SPEC is None or _PREFLIGHT_SPEC.loader is None:
    raise RuntimeError("ASAAS build trust preflight module is unavailable")
_PREFLIGHT = importlib.util.module_from_spec(_PREFLIGHT_SPEC)
sys.modules[_PREFLIGHT_SPEC.name] = _PREFLIGHT
_PREFLIGHT_SPEC.loader.exec_module(_PREFLIGHT)


class AttestationCliError(ValueError):
    """An administrator pin or CI provenance field is absent or invalid."""


def _load_envelope_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "src/services/billing_signed_envelope.py"
    )
    spec = importlib.util.spec_from_file_location(
        "asaas_build_attestation_envelope", module_path
    )
    if spec is None or spec.loader is None:
        raise AttestationCliError("canonical signing module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _required(environment: dict[str, str], key: str) -> str:
    value = environment.get(key, "")
    if not value or value.strip() != value:
        raise AttestationCliError("administrator trust pins are incomplete")
    return value


def _hex_digest(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _expected_oidc_subject(pins: dict[str, str]) -> str:
    repository = pins["GITHUB_REPOSITORY"]
    owner, _repository_name = repository.split("/", maxsplit=1)
    owner_id = pins["GITHUB_REPOSITORY_OWNER_ID"]
    repository_id = pins["GITHUB_REPOSITORY_ID"]
    workflow_path = pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"]
    workflow_ref = pins["BILLING_PRODUCER_BUILD_WORKFLOW_REF"]
    return (
        f"repo:{owner}@{owner_id}/{_repository_name}@{repository_id}:"
        f"environment:asaas-build:ref:{workflow_ref}:workflow_ref:"
        f"{repository}/{workflow_path}@{workflow_ref}"
    )


def _validate_administrator_pins(environment: dict[str, str]) -> dict[str, str]:
    names = (
        "BILLING_OPERATION_ID",
        "BILLING_PRODUCER_BUILD_ISSUER",
        "BILLING_PRODUCER_BUILD_KMS_KEY_ARN",
        "BILLING_PRODUCER_BUILD_AUDIENCE",
        "BILLING_PRODUCER_BUILD_ROLE_ARN",
        "BILLING_PRODUCER_BUILD_REPOSITORY_ID",
        "BILLING_PRODUCER_BUILD_REPOSITORY_NAME",
        "BILLING_PRODUCER_BUILD_WORKFLOW_PATH",
        "BILLING_PRODUCER_BUILD_WORKFLOW_REF",
        "BILLING_PRODUCER_BUILD_OIDC_ISSUER",
        "BILLING_PRODUCER_BUILD_OIDC_SUBJECT",
        "BILLING_PRODUCER_ECR_REPOSITORY_URI",
        "GITHUB_REPOSITORY_ID",
        "GITHUB_REPOSITORY_OWNER_ID",
        "GITHUB_REPOSITORY",
        "GITHUB_REF",
        "GITHUB_WORKFLOW_REF",
    )
    pins = {name: _required(environment, name) for name in names}
    public_key_pem = environment.get("BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM", "")
    if not public_key_pem.strip():
        raise AttestationCliError("administrator trust pins are incomplete")
    pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"] = public_key_pem
    try:
        operation_id = UUID(pins["BILLING_OPERATION_ID"])
    except ValueError:
        raise AttestationCliError("producer operation ID is invalid") from None
    if str(operation_id) != pins["BILLING_OPERATION_ID"]:
        raise AttestationCliError("producer operation ID is not canonical")
    if pins["GITHUB_REPOSITORY_ID"] != pins["BILLING_PRODUCER_BUILD_REPOSITORY_ID"]:
        raise AttestationCliError("repository ID differs from administrator pin")
    if pins["GITHUB_REPOSITORY"] != pins["BILLING_PRODUCER_BUILD_REPOSITORY_NAME"]:
        raise AttestationCliError("repository name differs from administrator pin")
    if not re.fullmatch(r"[1-9][0-9]*", pins["GITHUB_REPOSITORY_ID"]):
        raise AttestationCliError("repository ID is invalid")
    if not re.fullmatch(r"[1-9][0-9]*", pins["GITHUB_REPOSITORY_OWNER_ID"]):
        raise AttestationCliError("repository owner ID is invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", pins["GITHUB_REPOSITORY"]):
        raise AttestationCliError("repository name is invalid")
    workflow_path = pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"]
    workflow_ref = pins["BILLING_PRODUCER_BUILD_WORKFLOW_REF"]
    if workflow_path != ".github/workflows/asaas-build-attestation.yml":
        raise AttestationCliError(
            "workflow path is not the protected attestation workflow"
        )
    if not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", workflow_ref):
        raise AttestationCliError("workflow ref pin is invalid")
    if pins["GITHUB_REF"] != workflow_ref:
        raise AttestationCliError("workflow ref differs from administrator pin")
    expected_workflow_ref = (
        f"{pins['GITHUB_REPOSITORY']}/{workflow_path}@{workflow_ref}"
    )
    if pins["GITHUB_WORKFLOW_REF"] != expected_workflow_ref:
        raise AttestationCliError("workflow identity differs from administrator pin")
    if pins["BILLING_PRODUCER_BUILD_OIDC_ISSUER"] != (
        "https://token.actions.githubusercontent.com"
    ):
        raise AttestationCliError(
            "OIDC issuer differs from the protected GitHub issuer"
        )
    expected_oidc_subject = _expected_oidc_subject(pins)
    if pins["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"] != expected_oidc_subject:
        raise AttestationCliError(
            "OIDC subject differs from the immutable protected build identity"
        )
    if not re.fullmatch(
        r"arn:aws:iam::388460142431:role/[A-Za-z0-9+=,.@_/-]+",
        pins["BILLING_PRODUCER_BUILD_ROLE_ARN"],
    ):
        raise AttestationCliError("CI role ARN is invalid")
    if not re.fullmatch(
        r"arn:aws:kms:us-east-1:388460142431:key/[A-Za-z0-9-]+",
        pins["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
    ):
        raise AttestationCliError("CI KMS key ARN is invalid")
    if not re.fullmatch(
        r"388460142431\.dkr\.ecr\.us-east-1\.amazonaws\.com/[A-Za-z0-9._/-]+",
        pins["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
    ):
        raise AttestationCliError("ECR repository is outside the pinned account")
    if (
        pins["BILLING_PRODUCER_BUILD_ISSUER"].strip()
        != pins["BILLING_PRODUCER_BUILD_ISSUER"]
        or pins["BILLING_PRODUCER_BUILD_AUDIENCE"].strip()
        != pins["BILLING_PRODUCER_BUILD_AUDIENCE"]
    ):
        raise AttestationCliError("administrator trust pins are incomplete")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
    except (ImportError, ValueError, UnicodeEncodeError):
        raise AttestationCliError("pinned build public key is invalid") from None
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        raise AttestationCliError("pinned build public key must be P-256")
    return pins


def _validate_context(environment: dict[str, str]) -> dict[str, str]:
    names = (
        "BILLING_OPERATION_ID",
        "BILLING_PRODUCER_BUILD_ISSUER",
        "BILLING_PRODUCER_BUILD_KMS_KEY_ARN",
        "BILLING_PRODUCER_BUILD_AUDIENCE",
        "BILLING_PRODUCER_BUILD_ROLE_ARN",
        "BILLING_PRODUCER_BUILD_REPOSITORY_ID",
        "BILLING_PRODUCER_BUILD_REPOSITORY_NAME",
        "BILLING_PRODUCER_BUILD_WORKFLOW_PATH",
        "BILLING_PRODUCER_BUILD_WORKFLOW_REF",
        "BILLING_PRODUCER_BUILD_OIDC_ISSUER",
        "BILLING_PRODUCER_BUILD_OIDC_SUBJECT",
        "BILLING_PRODUCER_ECR_REPOSITORY_URI",
        "GITHUB_REPOSITORY_ID",
        "GITHUB_REPOSITORY_OWNER_ID",
        "GITHUB_REPOSITORY",
        "GITHUB_REF",
        "GITHUB_WORKFLOW_REF",
        "GITHUB_RUN_ID",
        "GITHUB_SHA",
        "GIT_COMMIT_OBJECT_SHA256",
        "GIT_TREE_SHA",
        "BUNDLE_DIGEST",
        "APP_IMAGE_DIGEST",
    )
    pins = {name: _required(environment, name) for name in names}
    pins.update(_validate_administrator_pins(environment))
    try:
        UUID(pins["BILLING_OPERATION_ID"])
    except ValueError:
        raise AttestationCliError("producer operation ID is invalid") from None
    if pins["GITHUB_REPOSITORY_ID"] != pins["BILLING_PRODUCER_BUILD_REPOSITORY_ID"]:
        raise AttestationCliError("repository ID differs from administrator pin")
    if pins["GITHUB_REPOSITORY"] != pins["BILLING_PRODUCER_BUILD_REPOSITORY_NAME"]:
        raise AttestationCliError("repository name differs from administrator pin")
    workflow_path = pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"]
    workflow_ref = pins["BILLING_PRODUCER_BUILD_WORKFLOW_REF"]
    if workflow_path != ".github/workflows/asaas-build-attestation.yml":
        raise AttestationCliError(
            "workflow path is not the protected attestation workflow"
        )
    if not re.fullmatch(r"refs/heads/[A-Za-z0-9._/-]+", workflow_ref):
        raise AttestationCliError("workflow ref pin is invalid")
    if pins["GITHUB_REF"] != workflow_ref:
        raise AttestationCliError("workflow ref differs from administrator pin")
    expected_workflow_ref = (
        f"{pins['GITHUB_REPOSITORY']}/{workflow_path}@{workflow_ref}"
    )
    if pins["GITHUB_WORKFLOW_REF"] != expected_workflow_ref:
        raise AttestationCliError("workflow identity differs from administrator pin")
    if pins["BILLING_PRODUCER_BUILD_OIDC_ISSUER"] != (
        "https://token.actions.githubusercontent.com"
    ):
        raise AttestationCliError(
            "OIDC issuer differs from the protected GitHub issuer"
        )
    expected_oidc_subject = _expected_oidc_subject(pins)
    if pins["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"] != expected_oidc_subject:
        raise AttestationCliError(
            "OIDC subject differs from the immutable protected build identity"
        )
    if not re.fullmatch(r"[1-9][0-9]*", pins["GITHUB_REPOSITORY_OWNER_ID"]):
        raise AttestationCliError("repository owner ID is invalid")
    if not re.fullmatch(r"[1-9][0-9]*", pins["GITHUB_REPOSITORY_ID"]):
        raise AttestationCliError("repository ID is invalid")
    if not re.fullmatch(r"[1-9][0-9]*", pins["GITHUB_RUN_ID"]):
        raise AttestationCliError("workflow run ID is invalid")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pins["GITHUB_SHA"]):
        raise AttestationCliError("GitHub commit ID is invalid")
    if not _hex_digest(pins["GIT_COMMIT_OBJECT_SHA256"]):
        raise AttestationCliError("commit object digest is invalid")
    if not _hex_digest(pins["GIT_TREE_SHA"]):
        raise AttestationCliError("tree digest is invalid")
    if not _hex_digest(pins["BUNDLE_DIGEST"]):
        raise AttestationCliError("bundle digest is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", pins["APP_IMAGE_DIGEST"]):
        raise AttestationCliError("application image digest is invalid")
    if not re.fullmatch(
        r"[0-9]{12}\.dkr\.ecr\.us-east-1\.amazonaws\.com/[A-Za-z0-9._/-]+",
        pins["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
    ):
        raise AttestationCliError("ECR repository URI is invalid")
    if not pins["BILLING_PRODUCER_ECR_REPOSITORY_URI"].startswith(
        "388460142431.dkr.ecr.us-east-1.amazonaws.com/"
    ):
        raise AttestationCliError("ECR repository is outside the pinned account")
    if not re.fullmatch(
        r"arn:aws:iam::388460142431:role/[A-Za-z0-9+=,.@_/-]+",
        pins["BILLING_PRODUCER_BUILD_ROLE_ARN"],
    ):
        raise AttestationCliError("CI role ARN is invalid")
    if not re.fullmatch(
        r"arn:aws:kms:us-east-1:388460142431:key/[A-Za-z0-9-]+",
        pins["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
    ):
        raise AttestationCliError("CI KMS key ARN is invalid")
    return pins


def _backend_trust_environment(
    pins: dict[str, str], environment: dict[str, str]
) -> dict[str, str]:
    return {
        "PIN_BACKEND_BASE_URL": environment.get("PIN_BACKEND_BASE_URL", ""),
        "ASAAS_PREFLIGHT_BACKEND_ORIGIN": environment.get(
            "ASAAS_PREFLIGHT_BACKEND_ORIGIN", ""
        ),
        "PIN_ISSUER": pins["BILLING_PRODUCER_BUILD_ISSUER"],
        "PIN_KMS_KEY_ARN": pins["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
        "PIN_BUILD_AUDIENCE": pins["BILLING_PRODUCER_BUILD_AUDIENCE"],
        "PIN_REPOSITORY_ID": pins["BILLING_PRODUCER_BUILD_REPOSITORY_ID"],
        "PIN_REPOSITORY_NAME": pins["BILLING_PRODUCER_BUILD_REPOSITORY_NAME"],
        "PIN_WORKFLOW_PATH": pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"],
        "PIN_WORKFLOW_REF": pins["BILLING_PRODUCER_BUILD_WORKFLOW_REF"],
        "PIN_OIDC_ISSUER": pins["BILLING_PRODUCER_BUILD_OIDC_ISSUER"],
        "PIN_OIDC_SUBJECT": pins["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"],
        "BUILD_PUBLIC_KEY": pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"],
    }


def _validate_bootstrap_pins(pins: dict[str, str], environment: dict[str, str]) -> None:
    trust_environment = _backend_trust_environment(pins, environment)
    trust_environment.update(
        {
            "PIN_ROLE_ARN": pins["BILLING_PRODUCER_BUILD_ROLE_ARN"],
            "PIN_ECR_URI": pins["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
        }
    )
    _PREFLIGHT._bootstrap_pins(trust_environment)


def _trust_entry_from_pins(pins: dict[str, str]) -> Any:
    module = _load_envelope_module()
    return module.TrustEntry(
        issuer=pins["BILLING_PRODUCER_BUILD_ISSUER"],
        kid=pins["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
        environment="staging",
        audience=pins["BILLING_PRODUCER_BUILD_AUDIENCE"],
        type="billing_build_attestation.v1",
        purpose="billing-build-attestation.v1",
        schema_version="v1",
        algorithm="ECDSA_SHA_256",
        public_key_pem=pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"],
        repository_id=pins["BILLING_PRODUCER_BUILD_REPOSITORY_ID"],
        repository_name=pins["BILLING_PRODUCER_BUILD_REPOSITORY_NAME"],
        workflow_path=pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"],
        workflow_ref=pins["BILLING_PRODUCER_BUILD_WORKFLOW_REF"],
        oidc_issuer=pins["BILLING_PRODUCER_BUILD_OIDC_ISSUER"],
        oidc_subject=pins["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"],
    )


def _trust_entry_from_backend_readback(
    pins: dict[str, str], readback: dict[str, str]
) -> Any:
    module = _load_envelope_module()
    return module.TrustEntry(
        issuer=readback["issuer"],
        kid=readback["kid"],
        environment=readback["environment"],
        audience=readback["audience"],
        type=readback["type"],
        purpose=readback["purpose"],
        schema_version=readback["schema_version"],
        algorithm=readback["algorithm"],
        public_key_pem=pins["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"],
        repository_id=readback["repository_id"],
        repository_name=readback["repository_name"],
        workflow_path=readback["workflow_path"],
        workflow_ref=readback["workflow_ref"],
        oidc_issuer=readback["oidc_issuer"],
        oidc_subject=readback["oidc_subject"],
    )


def _active_backend_trust(
    pins: dict[str, str], environment: dict[str, str]
) -> tuple[Any, str]:
    mode = _PREFLIGHT.trust_mode(environment)
    if environment.get("ASAAS_PREFLIGHT_BUILD_TRUST_MODE") != mode:
        raise AttestationCliError("build trust mode differs from preflight evidence")
    trust_environment = _backend_trust_environment(pins, environment)
    if mode == "normal":
        expected_digest = _required(environment, "ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256")
        readback, digest = _PREFLIGHT.fetch_backend_trust_readback(
            trust_environment, expected_digest=expected_digest
        )
        return _trust_entry_from_backend_readback(pins, readback), digest

    if environment.get("GITHUB_RUN_ATTEMPT") != "1":
        raise AttestationCliError("bootstrap mode requires the first run attempt")
    _validate_bootstrap_pins(pins, environment)
    expected_probe = _required(
        environment, "ASAAS_PREFLIGHT_BACKEND_TRUST_PROBE_SHA256"
    )
    try:
        digest = _PREFLIGHT.probe_backend_trust_readback_unavailable(
            trust_environment, expected_digest=expected_probe
        )
    except _PREFLIGHT.GateError as exc:
        raise AttestationCliError(str(exc)) from None
    return _trust_entry_from_pins(pins), digest


def _create_envelope(
    pins: dict[str, str],
    *,
    signer: object,
    trust_entry: Any,
    now: datetime | None = None,
) -> dict[str, object]:
    module = _load_envelope_module()
    now = now or datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)
    operation_id = UUID(pins["BILLING_OPERATION_ID"])
    attestation_id = uuid4()
    repository_id = pins["GITHUB_REPOSITORY_ID"]
    workflow_path = pins["BILLING_PRODUCER_BUILD_WORKFLOW_PATH"]
    app_digest = pins["APP_IMAGE_DIGEST"]
    payload = {
        "attestation_type": "billing_build_attestation.v1",
        "attestation_id": str(attestation_id),
        "repository_id": repository_id,
        "repository_name": pins["GITHUB_REPOSITORY"],
        "workflow_path": workflow_path,
        "workflow_ref": pins["GITHUB_REF"],
        "workflow_run_id": int(pins["GITHUB_RUN_ID"]),
        "app_image_digest": app_digest,
        "bundle_digest": pins["BUNDLE_DIGEST"],
        "commit_sha": pins["GIT_COMMIT_OBJECT_SHA256"],
        "tree_sha": pins["GIT_TREE_SHA"],
        "workflow_id": f"github-actions:{repository_id}:{workflow_path}",
        "immutable_artifact_reference": (
            f"{pins['BILLING_PRODUCER_ECR_REPOSITORY_URI']}@{app_digest}"
        ),
    }
    # KMS signing keys are asymmetric; only the returned signature is placed in
    # the artifact. The backend readback supplies the verification trust tuple.
    return module.build_envelope(
        entry=trust_entry,
        payload=payload,
        operation_id=operation_id,
        issued_at=now,
        expires_at=expires_at,
        signer=signer,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    parser.add_argument(
        "--validate-pins",
        action="store_true",
        help="validate all administrator and GitHub identity pins without signing",
    )
    args = parser.parse_args()
    try:
        if args.validate_pins:
            environment = dict(os.environ)
            pins = _validate_administrator_pins(environment)
            _active_backend_trust(pins, environment)
            return 0
        if not args.output:
            raise AttestationCliError("output path is required")
        pins = _validate_context(dict(os.environ))
        trust_entry, _trust_digest = _active_backend_trust(pins, dict(os.environ))
        output = Path(args.output)
        runner_temp = Path(_required(dict(os.environ), "RUNNER_TEMP")).resolve()
        if (
            output.exists()
            or output.is_symlink()
            or runner_temp not in output.resolve().parents
        ):
            raise AttestationCliError("output path is not a new runner-temporary file")
        import boto3
        from botocore.config import Config
        from botocore.exceptions import BotoCoreError, ClientError

        kms_client = boto3.client(
            "kms",
            region_name="us-east-1",
            config=Config(
                connect_timeout=2,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )
        module = _load_envelope_module()
        envelope = _create_envelope(
            pins,
            signer=module.Boto3KmsDigestSigner(kms_client),
            trust_entry=trust_entry,
        )
        verified_entry = module.verify_envelope(
            envelope,
            module.TrustRegistry([trust_entry]),
            expected_type="billing_build_attestation.v1",
        )
        module.validate_attestation_trust(verified_entry, envelope)
        output.write_text(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(output, 0o600)
        return 0
    except AttestationCliError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ImportError:
        print(
            "canonical build attestation signing dependencies are unavailable",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, BotoCoreError, ClientError):
        # Keep AWS/credential exception text out of CI logs.
        print("canonical build attestation signing failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
