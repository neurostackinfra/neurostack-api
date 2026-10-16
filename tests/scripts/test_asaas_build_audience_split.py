from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github/workflows/asaas-build-attestation.yml"
_PREFLIGHT = _ROOT / "scripts/asaas_build_release_preflight.py"


def _preflight_module():
    spec = importlib.util.spec_from_file_location(
        "asaas_build_preflight_audience_test", _PREFLIGHT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_scope() -> dict[str, str]:
    return {
        "INPUT_WISH": "asaas-producer-image-rollout",
        "INPUT_GROUP": "3",
        "INPUT_AUTHORIZATION_REFERENCE": (
            "asaas-producer-image-rollout/WISH.md#"
            "2026-10-15-separate-build-and-rollout-plan-approved"
        ),
        "INPUT_OPERATION_ID": "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
        "GITHUB_REPOSITORY": "neurostackinfra/neurostack-api",
        "GITHUB_REPOSITORY_ID": "1152335815",
        "GITHUB_REPOSITORY_OWNER_ID": "340698182",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": (
            "neurostackinfra/neurostack-api/"
            ".github/workflows/asaas-build-attestation.yml@refs/heads/main"
        ),
        "PIN_REPOSITORY_ID": "1152335815",
        "PIN_REPOSITORY_NAME": "neurostackinfra/neurostack-api",
        "PIN_WORKFLOW_PATH": ".github/workflows/asaas-build-attestation.yml",
        "PIN_WORKFLOW_REF": "refs/heads/main",
        "PIN_ROLE_ARN": "arn:aws:iam::388460142431:role/asaas-build",
        "PIN_KMS_KEY_ARN": (
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        "PIN_BUILD_AUDIENCE": "billing-api:producer-build-attestation",
        "PIN_OIDC_AUDIENCE": "sts.amazonaws.com",
        "PIN_ISSUER": "asaas-build-issuer",
        "PIN_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "PIN_OIDC_SUBJECT": (
            "repo:neurostackinfra@340698182/neurostack-api@1152335815:environment:"
            "asaas-build:ref:refs/heads/main:workflow_ref:"
            "neurostackinfra/neurostack-api/.github/workflows/"
            "asaas-build-attestation.yml@refs/heads/main"
        ),
        "PIN_ECR_URI": ("388460142431.dkr.ecr.us-east-1.amazonaws.com/asaas-producer"),
        "BUILD_PUBLIC_KEY": "pinned-public-key",
    }


def test_preflight_accepts_independent_application_and_aws_oidc_audiences():
    module = _preflight_module()
    env = _valid_scope()

    module.validate_scope(env)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("PIN_BUILD_AUDIENCE", "", "build_audience_pin_invalid"),
        ("PIN_BUILD_AUDIENCE", " billing-api:producer ", "build_audience_pin_invalid"),
        ("PIN_BUILD_AUDIENCE", "sts.amazonaws.com", "build_audience_pin_invalid"),
        ("PIN_OIDC_AUDIENCE", "", "oidc_audience_pin_mismatch"),
        (
            "PIN_OIDC_AUDIENCE",
            "billing-api:producer-build-attestation",
            "oidc_audience_pin_mismatch",
        ),
    ],
)
def test_preflight_rejects_missing_or_conflated_audience_pins(
    field: str, value: str, reason: str
):
    module = _preflight_module()
    env = _valid_scope()
    env[field] = value

    with pytest.raises(module.GateError, match=reason):
        module.validate_scope(env)


def test_workflow_uses_oidc_audience_only_for_aws_and_build_audience_for_envelope():
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    preflight = next(
        step for step in jobs["preflight"]["steps"] if step.get("id") == "gate"
    )
    assert preflight["env"]["PIN_BUILD_AUDIENCE"] == (
        "${{ vars.BILLING_PRODUCER_BUILD_AUDIENCE }}"
    )
    assert preflight["env"]["PIN_OIDC_AUDIENCE"] == (
        "${{ vars.BILLING_PRODUCER_BUILD_OIDC_AUDIENCE }}"
    )

    build_steps = jobs["build-attestation"]["steps"]
    validate = next(
        step
        for step in build_steps
        if step.get("name") == "Validate signer pins before requesting OIDC credentials"
    )
    assert validate["env"]["BILLING_PRODUCER_BUILD_AUDIENCE"] == (
        "${{ vars.BILLING_PRODUCER_BUILD_AUDIENCE }}"
    )
    assert validate["env"]["BILLING_PRODUCER_BUILD_OIDC_AUDIENCE"] == (
        "${{ vars.BILLING_PRODUCER_BUILD_OIDC_AUDIENCE }}"
    )
    assert (
        'test "$BILLING_PRODUCER_BUILD_OIDC_AUDIENCE" = "sts.amazonaws.com"'
        in validate["run"]
    )

    credentials = next(
        step
        for step in build_steps
        if "configure-aws-credentials@" in step.get("uses", "")
    )
    assert credentials["with"]["audience"] == (
        "${{ vars.BILLING_PRODUCER_BUILD_OIDC_AUDIENCE }}"
    )
    assert "BILLING_PRODUCER_BUILD_AUDIENCE" not in credentials["with"]["audience"]

    sign = next(
        step
        for step in build_steps
        if step.get("name") == "Sign and verify the canonical CI attestation"
    )
    assert sign["env"]["BILLING_PRODUCER_BUILD_AUDIENCE"] == (
        "${{ vars.BILLING_PRODUCER_BUILD_AUDIENCE }}"
    )
    assert "BILLING_PRODUCER_BUILD_OIDC_AUDIENCE" not in sign["env"]
