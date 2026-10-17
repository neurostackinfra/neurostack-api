from __future__ import annotations

import re
import shlex
from pathlib import Path

from scripts import validate_asaas_producer_release_readbacks as validator

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "validate-production-infra.sh"
TEMPLATE = ROOT / "infra" / "production" / "asaas-producer-release.yaml"
EXPECTED_TESTS = (
    "tests/scripts/test_production_infra.py",
    "tests/scripts/test_asaas_producer_release_readbacks.py",
    "tests/scripts/test_asaas_backend_trust_readback.py",
    "tests/scripts/test_asaas_build_workflow.py",
    "tests/scripts/test_sign_asaas_build_attestation.py",
    "tests/scripts/test_asaas_release_preflight_workflow.py",
)


def test_production_infra_gate_is_asaas_only_and_runs_static_template_first() -> None:
    source = GATE.read_text(encoding="utf-8")
    assert "set -euo pipefail" in source
    assert (
        "scripts/validate_asaas_producer_release_readbacks.py --check-template"
        in source
    )
    assert "-m pytest -q" in source
    assert all(path in source for path in EXPECTED_TESTS)
    assert "tests/" in source
    assert not re.search(
        r"\b(?:aws|gh)\s+(?:deploy|create|update|put|register)", source
    )
    assert "id-token" not in source.lower()

    commands = [
        shlex.split(line.strip())
        for line in source.replace("\\\n", " ").splitlines()
        if line.strip().startswith('"$python_bin" ')
    ]
    assert commands[0] == [
        "$python_bin",
        "scripts/validate_asaas_producer_release_readbacks.py",
        "--check-template",
    ]
    assert commands[1][:3] == ["$python_bin", "-m", "pytest"]
    assert commands[1][3:] == ["-q", *EXPECTED_TESTS]


def test_template_and_static_validator_enforce_asaas_trust_policy() -> None:
    template = validator._load_template(TEMPLATE)
    assert validator._template_static_errors(template) == []
    for role_name, expected_subject in (
        ("BuildRole", validator.BUILD_SUBJECT),
        ("DeployRole", validator.DEPLOY_SUBJECT),
    ):
        trust = template["Resources"][role_name]["Properties"][
            "AssumeRolePolicyDocument"
        ]
        statement = trust["Statement"][0]
        assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"
        assert statement["Principal"] == {"Federated": validator.OIDC_PROVIDER_ARN}
        assert statement["Condition"]["StringEquals"] == {
            "token.actions.githubusercontent.com:aud": validator.OIDC_AUDIENCE,
            "token.actions.githubusercontent.com:sub": expected_subject,
        }
        assert "*" not in str(statement["Condition"])


def test_static_validator_rejects_widened_trust_policy() -> None:
    template = validator._load_template(TEMPLATE)
    template["Resources"]["BuildRole"]["Properties"]["AssumeRolePolicyDocument"][
        "Statement"
    ][0]["Condition"]["StringEquals"][
        "token.actions.githubusercontent.com:sub"
    ] = "repo:neurostackinfra/*"
    assert any(
        "wildcard_trust" in error
        for error in validator._template_static_errors(template)
    )
