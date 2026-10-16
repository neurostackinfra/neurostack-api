from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import re
import struct
import subprocess
import zlib
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "asaas-build-attestation.yml"
)
_PREFLIGHT_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "asaas_build_release_preflight.py"
)
_APPROVAL_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "verify_asaas_build_environment_approval.py"
)
_REFERENCE = (
    "asaas-producer-image-rollout/WISH.md#"
    "2026-10-15-separate-build-and-rollout-plan-approved"
)
_EXPECTED_ACTION_PINS = {
    "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
    "aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
}
_VALID_EVIDENCE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae426082"
)
_EVIDENCE_URL = (
    "https://github.com/user-attachments/assets/" "12345678-1234-4abc-8def-0123456789ab"
)


def _workflow() -> tuple[str, dict]:
    source = _WORKFLOW.read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def _gate_namespace() -> dict:
    namespace = {"__name__": "asaas_build_workflow_test"}
    exec(
        compile(
            _PREFLIGHT_SCRIPT.read_text(encoding="utf-8"),
            str(_PREFLIGHT_SCRIPT),
            "exec",
        ),
        namespace,
    )
    return namespace


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + kind
        + data
        + (zlib.crc32(kind + data) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def _png_before_idat(*chunks: bytes) -> bytes:
    verifier_signature = b"\x89PNG\r\n\x1a\n"
    return (
        verifier_signature
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + b"".join(chunks)
        + _png_chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0"))
        + _png_chunk(b"IEND", b"")
    )


def _indexed_png(sample_byte: int, palette_entries: int = 2) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 1, 3, 0, 0, 0))
        + _png_chunk(
            b"PLTE",
            bytes(
                channel for index in range(palette_entries) for channel in (index, 0, 0)
            ),
        )
        + _png_chunk(b"IDAT", zlib.compress(bytes((0, sample_byte))))
        + _png_chunk(b"IEND", b"")
    )


def _approval_module():
    spec = importlib.util.spec_from_file_location(
        "asaas_build_approval_test", _APPROVAL_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_scope() -> dict[str, str]:
    repository = "neurostackinfra/neurostack-api"
    workflow_path = ".github/workflows/asaas-build-attestation.yml"
    workflow_ref = "refs/heads/main"
    return {
        "INPUT_OPERATION_ID": "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
        "INPUT_WISH": "asaas-producer-image-rollout",
        "INPUT_GROUP": "3",
        "INPUT_AUTHORIZATION_REFERENCE": _REFERENCE,
        "GITHUB_REPOSITORY": repository,
        "GITHUB_REPOSITORY_ID": "1152335815",
        "GITHUB_REPOSITORY_OWNER_ID": "340698182",
        "GITHUB_REF": workflow_ref,
        "GITHUB_WORKFLOW_REF": f"{repository}/{workflow_path}@{workflow_ref}",
        "PIN_REPOSITORY_ID": "1152335815",
        "PIN_REPOSITORY_NAME": repository,
        "PIN_WORKFLOW_PATH": workflow_path,
        "PIN_WORKFLOW_REF": workflow_ref,
        "PIN_ROLE_ARN": "arn:aws:iam::388460142431:role/asaas-build",
        "PIN_KMS_KEY_ARN": (
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        "PIN_BUILD_AUDIENCE": "billing-api:producer-build-attestation",
        "PIN_OIDC_AUDIENCE": "sts.amazonaws.com",
        "PIN_ISSUER": "asaas-build-test-issuer",
        "PIN_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "PIN_OIDC_SUBJECT": (
            "repo:neurostackinfra@340698182/neurostack-api@1152335815:environment:"
            "asaas-build:ref:refs/heads/main:workflow_ref:"
            "neurostackinfra/neurostack-api/.github/workflows/"
            "asaas-build-attestation.yml@refs/heads/main"
        ),
        "PIN_ECR_URI": ("388460142431.dkr.ecr.us-east-1.amazonaws.com/asaas-producer"),
        "PIN_BACKEND_BASE_URL": "https://staging.api.neurostack.global",
        "BUILD_PUBLIC_KEY": "pinned-public-key",
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


def _valid_bootstrap_scope(namespace: dict) -> dict[str, str]:
    env = _valid_scope()
    env.update(
        {
            "INPUT_GROUP": "4",
            "INPUT_TRUST_MODE": "bootstrap",
            "GITHUB_RUN_ATTEMPT": "1",
            "PIN_ROLE_ARN": namespace["BOOTSTRAP_BUILD_ROLE_ARN"],
            "PIN_KMS_KEY_ARN": namespace["BOOTSTRAP_KMS_KEY_ARN"],
            "PIN_ECR_URI": namespace["BOOTSTRAP_ECR_URI"],
            "PIN_ISSUER": namespace["BOOTSTRAP_BUILD_ISSUER"],
            "BUILD_PUBLIC_KEY": _bootstrap_public_key_pem(),
        }
    )
    return env


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("INPUT_WISH", "", "wish_scope_invalid"),
        ("INPUT_WISH", "asaas-payment-gateway", "wish_scope_invalid"),
        ("INPUT_WISH", "asaas-gateway-payment", "wish_scope_invalid"),
        ("INPUT_GROUP", "7b", "group_scope_invalid"),
        ("INPUT_GROUP", "", "group_scope_invalid"),
        ("INPUT_AUTHORIZATION_REFERENCE", "", "authorization_reference_unrecorded"),
        (
            "INPUT_AUTHORIZATION_REFERENCE",
            "different-wish",
            "authorization_reference_unrecorded",
        ),
        ("INPUT_OPERATION_ID", "", "operation_id_invalid"),
    ],
)
def test_scope_gate_rejects_missing_invalid_and_non_rollout_scope(
    field: str, value: str, reason: str
) -> None:
    namespace = _gate_namespace()
    env = _valid_scope()
    env[field] = value

    with pytest.raises(namespace["GateError"], match=reason):
        namespace["validate_scope"](env)


def test_scope_gate_accepts_only_documented_rollout_group_3_or_4() -> None:
    namespace = _gate_namespace()
    for group in ("3", "4"):
        env = _valid_scope()
        env["INPUT_GROUP"] = group
        namespace["validate_scope"](env)


def test_bootstrap_scope_requires_first_group_four_run_and_committed_kms_fingerprint():
    namespace = _gate_namespace()
    env = _valid_bootstrap_scope(namespace)
    namespace["validate_scope"](env)

    for key, value, reason in (
        ("INPUT_GROUP", "3", "bootstrap_group_scope_invalid"),
        ("GITHUB_RUN_ATTEMPT", "2", "bootstrap_run_attempt_must_be_first"),
        ("PIN_ISSUER", "uncommitted-issuer", "bootstrap_trust_pin_mismatch"),
        (
            "PIN_KMS_KEY_ARN",
            "arn:aws:kms:us-east-1:388460142431:key/other",
            "bootstrap_trust_pin_mismatch",
        ),
        (
            "BUILD_PUBLIC_KEY",
            "-----BEGIN PUBLIC KEY-----\ninvalid\n-----END PUBLIC KEY-----",
            "build_public_key_pin_invalid",
        ),
    ):
        invalid = dict(env)
        invalid[key] = value
        with pytest.raises(namespace["GateError"], match=reason):
            namespace["validate_scope"](invalid)


def test_bootstrap_probe_accepts_only_the_known_absent_endpoint_and_binds_digest():
    namespace = _gate_namespace()
    env = _valid_bootstrap_scope(namespace)
    url = "https://staging.api.neurostack.global/v1/admin/billing/producer-build-trust"
    body = (
        b'{"error":"unauthorized","message":"Missing Authorization or '
        b'X-API-Key header","documentation_url":"https://docs.neurostack.ai/auth"}'
    )
    headers = Message()
    headers["Content-Type"] = "application/json"
    bodies = []

    def opener(_request, *, timeout):
        assert timeout == 5
        response_body = io.BytesIO(body)
        bodies.append(response_body)
        return_value = HTTPError(url, 401, "Unauthorized", headers, response_body)
        raise return_value

    digest = namespace["probe_backend_trust_absent"](env, opener=opener)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert (
        namespace["probe_backend_trust_absent"](
            env, opener=opener, expected_digest=digest
        )
        == digest
    )
    assert all(response_body.closed for response_body in bodies)

    duplicate_key_body = (
        b'{"error":"unauthorized","error":"ok","message":"Missing '
        b'Authorization or X-API-Key header",'
        b'"documentation_url":"https://docs.neurostack.ai/auth"}'
    )

    def duplicate_key_opener(_request, *, timeout):
        raise HTTPError(
            url, 401, "Unauthorized", headers, io.BytesIO(duplicate_key_body)
        )

    with pytest.raises(
        namespace["GateError"], match="bootstrap_trust_absence_unconfirmed"
    ):
        namespace["probe_backend_trust_absent"](env, opener=duplicate_key_opener)

    changed_body = body.replace(b"X-API-Key", b"X-Other-Key")

    def changed_opener(_request, *, timeout):
        raise HTTPError(url, 401, "Unauthorized", headers, io.BytesIO(changed_body))

    with pytest.raises(namespace["GateError"], match="bootstrap_trust_probe_changed"):
        namespace["probe_backend_trust_absent"](
            env, opener=changed_opener, expected_digest=digest
        )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (200, "bootstrap_trust_already_active"),
        (302, "backend_trust_readback_redirect_rejected"),
        (404, "bootstrap_trust_absence_unconfirmed"),
        (503, "bootstrap_trust_absence_unconfirmed"),
    ],
)
def test_bootstrap_probe_fails_closed_for_active_redirect_missing_and_unavailable(
    status: int, reason: str
) -> None:
    namespace = _gate_namespace()
    env = _valid_bootstrap_scope(namespace)
    url = "https://staging.api.neurostack.global/v1/admin/billing/producer-build-trust"
    headers = Message()
    headers["Content-Type"] = "application/json"

    def opener(_request, *, timeout):
        if status == 200:

            class ActiveResponse:
                def __init__(self):
                    self.status = 200
                    self.headers = headers

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def geturl(self):
                    return url

                def read(self, _limit):
                    return b"{}"

            return ActiveResponse()
        raise HTTPError(url, status, "HTTP result", headers, io.BytesIO(b"{}"))

    with pytest.raises(namespace["GateError"], match=reason):
        namespace["probe_backend_trust_absent"](env, opener=opener)


def test_bootstrap_operation_history_rejects_prior_run_for_same_operation():
    namespace = _gate_namespace()
    operation = "89bf1556-9f6d-4f39-b5c8-8dcb041877da"
    runs = [
        {
            "id": 7123456789,
            "event": "workflow_dispatch",
            "display_title": f"ASAAS producer build bootstrap {operation}",
        },
        {
            "id": 7123456788,
            "event": "workflow_dispatch",
            "display_title": (
                "ASAAS producer build normal " "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            ),
        },
    ]
    namespace["validate_bootstrap_operation_unused"](
        "masked",
        operation,
        "7123456789",
        github_collection_fn=lambda *_args, **_kwargs: runs,
    )
    runs.append(
        {
            "id": 7123456787,
            "event": "workflow_dispatch",
            "display_title": f"ASAAS producer build normal {operation}",
        }
    )
    with pytest.raises(
        namespace["GateError"], match="bootstrap_operation_already_used"
    ):
        namespace["validate_bootstrap_operation_unused"](
            "masked",
            operation,
            "7123456789",
            github_collection_fn=lambda *_args, **_kwargs: runs,
        )


def test_bootstrap_operation_history_fails_closed_on_unattributable_legacy_title():
    namespace = _gate_namespace()
    with pytest.raises(
        namespace["GateError"], match="bootstrap_run_history_unattributable"
    ):
        namespace["validate_bootstrap_operation_unused"](
            "masked",
            "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
            "7123456789",
            github_collection_fn=lambda *_args, **_kwargs: [
                {
                    "id": 7123456788,
                    "event": "workflow_dispatch",
                    "display_title": "ASAAS producer build attestation",
                }
            ],
        )


def test_bootstrap_operation_history_cannot_skip_legacy_run_on_later_page():
    namespace = _gate_namespace()
    start_url = (
        "https://api.github.com/repos/neurostackinfra/neurostack-api/actions/"
        "workflows/asaas-build-attestation.yml/runs"
        "?event=workflow_dispatch&per_page=100&page=1"
    )
    second_url = start_url.replace("page=1", "page=2")
    seen = []

    def github_get(url, _token):
        seen.append(url)
        if url == start_url:
            return (
                {
                    "total_count": 2,
                    "workflow_runs": [
                        {
                            "id": 7123456788,
                            "event": "workflow_dispatch",
                            "display_title": (
                                "ASAAS producer build normal "
                                "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                            ),
                        }
                    ],
                },
                f'<{second_url}>; rel="next"',
            )
        if url == second_url:
            return (
                {
                    "total_count": 2,
                    "workflow_runs": [
                        {
                            "id": 7123456787,
                            "event": "workflow_dispatch",
                            "display_title": "ASAAS producer build attestation",
                        }
                    ],
                },
                None,
            )
        raise AssertionError("unexpected workflow-run page")

    namespace["github_get"] = github_get
    with pytest.raises(
        namespace["GateError"], match="bootstrap_run_history_unattributable"
    ):
        namespace["validate_bootstrap_operation_unused"](
            "masked",
            "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
            "7123456789",
        )
    assert seen == [start_url, second_url]


def _ruleset() -> dict:
    return {
        "id": 123,
        "name": "asaas-producer-release-main",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
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
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "ASAAS Release Preflight", "integration_id": None}
                    ],
                },
            },
            {"type": "deletion"},
            {"type": "non_fast_forward"},
        ],
    }


def test_ruleset_gate_rejects_omitted_bypass_and_weakened_controls() -> None:
    namespace = _gate_namespace()
    cases = []
    missing_bypass = _ruleset()
    missing_bypass.pop("bypass_actors")
    cases.append((missing_bypass, "ruleset_bypass_not_empty_or_unreadable"))
    allowed_bypass = _ruleset()
    allowed_bypass["bypass_actors"] = [{"actor_id": 1, "actor_type": "Organization"}]
    cases.append((allowed_bypass, "ruleset_bypass_not_empty_or_unreadable"))
    wrong_ref = _ruleset()
    wrong_ref["conditions"]["ref_name"]["include"].append("refs/heads/release/*")
    cases.append((wrong_ref, "ruleset_ref_scope_mismatch"))
    weak_pr = _ruleset()
    weak_pr["rules"][0]["parameters"]["require_last_push_approval"] = False
    cases.append((weak_pr, "ruleset_review_controls_weak"))
    no_checks = _ruleset()
    no_checks["rules"][1]["parameters"]["required_status_checks"] = []
    cases.append((no_checks, "ruleset_named_checks_missing"))
    no_deletion = _ruleset()
    no_deletion["rules"].remove({"type": "deletion"})
    cases.append((no_deletion, "ruleset_required_controls_missing"))

    for ruleset, reason in cases:
        namespace["github_get"] = lambda _url, _token, item=ruleset: (item, None)
        with pytest.raises(namespace["GateError"], match=reason):
            namespace["validate_ruleset"](
                [{"id": 123, "name": "asaas-producer-release-main"}], "masked"
            )


def _environment(name: str = "asaas-build") -> dict:
    return {
        "name": name,
        "can_admins_bypass": False,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "reviewers": [
                    {
                        "type": "User",
                        "reviewer": {"id": 42, "login": "felps-json"},
                    }
                ],
                "prevent_self_review": True,
            }
        ],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }


def test_environment_gate_accepts_documented_branch_policy_response_shape() -> None:
    namespace = _gate_namespace()
    environment = _environment()
    namespace["github_get"] = lambda _url, _token: (environment, None)
    namespace["github_collection"] = lambda *_args, **_kwargs: [
        {
            "id": 12345,
            "node_id": "BEP_kwDOABC123",
            "name": "main",
            "type": "branch",
        }
    ]
    readback = namespace["validate_environment"]("asaas-build", "masked")
    assert readback["reviewers"] == [{"type": "User", "id": 42, "login": "felps-json"}]
    assert readback["branch_policies"] == [
        {
            "id": 12345,
            "node_id": "BEP_kwDOABC123",
            "name": "main",
            "type": "branch",
        }
    ]


def test_environment_gate_rejects_missing_bypass_readback_and_non_branch_policy() -> (
    None
):
    namespace = _gate_namespace()
    environment = _environment()
    for mutation, reason in (
        (
            lambda value: value.pop("can_admins_bypass"),
            "environment_admin_bypass_enabled_or_unreadable",
        ),
        (
            lambda value: value.update(can_admins_bypass=True),
            "environment_admin_bypass_enabled_or_unreadable",
        ),
        (
            lambda value: value.update(
                deployment_branch_policy={
                    "protected_branches": True,
                    "custom_branch_policies": False,
                }
            ),
            "environment_branch_policy_unrestricted_or_mismatched",
        ),
    ):
        changed = copy.deepcopy(environment)
        mutation(changed)
        namespace["github_get"] = lambda _url, _token, item=changed: (item, None)
        with pytest.raises(namespace["GateError"], match=reason):
            namespace["validate_environment"]("asaas-build", "masked")

    namespace["github_get"] = lambda _url, _token: (_environment(), None)
    for bad_rows in (
        [{"id": 1, "node_id": "BEP_main", "name": "release"}],
        [
            {"id": 1, "node_id": "BEP_main", "name": "main"},
            {"id": 2, "node_id": "BEP_extra", "name": "release"},
        ],
        [{"id": 1, "node_id": "BEP_wildcard", "name": "release/*"}],
        [{"id": 1, "node_id": "BEP_tag", "name": "main", "type": "tag"}],
        [{"name": "main", "type": "tag"}],
        [{"name": "main", "type": "branch"}, {"name": "release/*", "type": "branch"}],
    ):
        namespace["github_collection"] = lambda *_args, rows=bad_rows, **_kwargs: rows
        with pytest.raises(namespace["GateError"]):
            namespace["validate_environment"]("asaas-build", "masked")


def test_preflight_writes_run_bound_admin_approval_instructions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = _gate_namespace()
    context = {
        "run_id": "7123456789",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
    }
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    namespace["write_admin_ui_approval_instructions"](
        context,
        "asaas-build",
        "c" * 64,
        "2026-10-16T05:00:00Z",
        "d" * 64,
        "https://staging.api.neurostack.global",
    )
    text = summary.read_text(encoding="utf-8")
    assert "Independent administrator UI review required before approval" in text
    assert "Run ID: `7123456789`" in text
    assert "Workflow: `.github/workflows/asaas-build-attestation.yml`" in text
    assert f"SHA: `{'a' * 40}`" in text
    assert "Ref: `refs/heads/main`" in text
    assert f"API readback SHA-256: `{'c' * 64}`" in text
    assert f"Backend build-trust readback SHA-256: `{'d' * 64}`" in text
    assert "Backend origin: `https://staging.api.neurostack.global`" in text
    assert "ASAAS-ADMIN-UI-ATTESTATION/2" in text
    assert "environment_admin_bypass=disabled" in text
    assert "attach it to a PR or issue comment" in text
    assert "evidence_ref=https://github.com/user-attachments/assets/<uuid>" in text

    for invalid, environment, digest, backend_digest, backend_origin in (
        (
            {**context, "run_id": ""},
            "asaas-build",
            "c" * 64,
            "d" * 64,
            "https://staging.api.neurostack.global",
        ),
        (
            {**context, "workflow_path": "other.yml"},
            "asaas-build",
            "c" * 64,
            "d" * 64,
            "https://staging.api.neurostack.global",
        ),
        (
            {**context, "sha": "short"},
            "asaas-build",
            "c" * 64,
            "d" * 64,
            "https://staging.api.neurostack.global",
        ),
        (
            {**context, "ref": "refs/heads/release"},
            "asaas-build",
            "c" * 64,
            "d" * 64,
            "https://staging.api.neurostack.global",
        ),
        (
            context,
            "asaas-production-deployment",
            "c" * 64,
            "d" * 64,
            "https://staging.api.neurostack.global",
        ),
        (
            context,
            "asaas-build",
            "invalid",
            "d" * 64,
            "https://staging.api.neurostack.global",
        ),
        (
            context,
            "asaas-build",
            "c" * 64,
            "invalid",
            "https://staging.api.neurostack.global",
        ),
        (
            None,
            "asaas-build",
            "c" * 64,
            "d" * 64,
            "https://staging.api.neurostack.global",
        ),
    ):
        with pytest.raises(
            namespace["GateError"], match="environment_approval_context_missing"
        ):
            namespace["write_admin_ui_approval_instructions"](
                invalid,
                environment,
                digest,
                "2026-10-16T05:00:00Z",
                backend_digest,
                backend_origin,
            )

    monkeypatch.delenv("GITHUB_STEP_SUMMARY")
    with pytest.raises(
        namespace["GateError"], match="environment_approval_context_missing"
    ):
        namespace["write_admin_ui_approval_instructions"](
            context,
            "asaas-build",
            "c" * 64,
            "2026-10-16T05:00:00Z",
            "d" * 64,
            "https://staging.api.neurostack.global",
        )


def test_preflight_emits_readback_digest_before_environment_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = _gate_namespace()
    now = datetime.now(timezone.utc)
    run_id = "7123456789"
    sha = "a" * 40
    repository = {
        "full_name": "neurostackinfra/neurostack-api",
        "id": 1152335815,
        "owner": {"id": 340698182},
        "default_branch": "main",
    }
    run = {
        "id": int(run_id),
        "event": "workflow_dispatch",
        "status": "in_progress",
        "head_sha": sha,
        "head_branch": "main",
        "path": ".github/workflows/asaas-build-attestation.yml@refs/heads/main",
        "repository": {"full_name": "neurostackinfra/neurostack-api", "id": 1152335815},
        "head_repository": {"id": 1152335815},
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }

    def github_get(url: str, _token: str) -> tuple[dict, None]:
        if url.endswith("/rulesets/123"):
            return _ruleset(), None
        if "/environments/" in url:
            name = url.rsplit("/", 1)[-1]
            return _environment(name), None
        if url.endswith(f"/actions/runs/{run_id}"):
            return run, None
        return repository, None

    def github_collection(url: str, _token: str, *, list_field=None) -> list[dict]:
        if "/rulesets?" in url:
            return [{"id": 123, "name": "asaas-producer-release-main"}]
        if "deployment-branch-policies" in url:
            return [
                {
                    "id": 12345,
                    "node_id": "BEP_kwDOABC123",
                    "name": "main",
                    "type": "branch",
                }
            ]
        assert list_field == "jobs"
        return [{"name": "preflight", "head_sha": sha, "status": "in_progress"}]

    namespace["github_get"] = github_get
    namespace["github_collection"] = github_collection
    namespace["fetch_backend_trust_readback"] = lambda _env: ({}, "d" * 64)
    for key, value in _valid_scope().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GH_TOKEN", "masked")
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_SHA", sha)
    output = tmp_path / "github-output"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    namespace["main"]()

    output_text = output.read_text(encoding="utf-8")
    assert "rollout_scope=validated" in output_text
    assert re.search(r"preflight_readback_sha256=[0-9a-f]{64}", output_text)
    assert re.search(r"backend_trust_readback_sha256=[0-9a-f]{64}", output_text)
    assert "backend_trust_origin=https://staging.api.neurostack.global" in output_text
    assert re.search(
        r"preflight_observed_at=202\d-[0-9]{2}-[0-9]{2}T[^\n]+Z", output_text
    )
    summary_text = summary.read_text(encoding="utf-8")
    assert (
        "Independent administrator UI review required before approval" in summary_text
    )
    assert "ASAAS-ADMIN-UI-ATTESTATION/2" in summary_text


def test_actions_run_gate_binds_current_run_workflow_ref_and_sha() -> None:
    namespace = _gate_namespace()
    now = datetime.now(timezone.utc)
    run = {
        "id": 7123456789,
        "event": "workflow_dispatch",
        "status": "in_progress",
        "head_sha": "a" * 40,
        "head_branch": "main",
        "path": (".github/workflows/asaas-build-attestation.yml@refs/heads/main"),
        "repository": {
            "full_name": "neurostackinfra/neurostack-api",
            "id": 1152335815,
        },
        "head_repository": {"id": 1152335815},
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }
    env = {"GITHUB_RUN_ID": "7123456789", "GITHUB_SHA": "a" * 40}
    urls: list[str] = []
    namespace["github_get"] = lambda url, _token: urls.append(url) or (run, None)
    namespace["github_collection"] = lambda *_args, **_kwargs: [
        {"name": "preflight", "head_sha": "a" * 40, "status": "in_progress"}
    ]

    namespace["validate_current_run"]("masked", env)
    assert urls == [
        "https://api.github.com/repos/neurostackinfra/neurostack-api/"
        "actions/runs/7123456789"
    ]
    assert namespace["validate_current_run"]("masked", env) == {
        "run_id": "7123456789",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
    }

    mutations = (
        lambda value: value.update(id=7123456790),
        lambda value: value.update(head_sha="b" * 40),
        lambda value: value.update(path=".github/workflows/other.yml@refs/heads/main"),
        lambda value: value.update(head_branch="release"),
        lambda value: value.update(created_at=(now - timedelta(hours=2)).isoformat()),
    )
    for mutation in mutations:
        invalid = copy.deepcopy(run)
        mutation(invalid)
        namespace["github_get"] = lambda _url, _token, item=invalid: (item, None)
        with pytest.raises(namespace["GateError"]):
            namespace["validate_current_run"]("masked", env)


def _approval_gate_fixture(now: datetime):
    verifier = _approval_module()
    run_id = "7123456789"
    sha = "a" * 40
    preflight_digest = "c" * 64
    backend_origin = "https://staging.api.neurostack.global"
    public_key = ec.generate_private_key(ec.SECP256R1()).public_key()
    public_key_pem = public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    public_key_fingerprint = hashlib.sha256(
        public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()
    backend_trust_env = {
        "PIN_BACKEND_BASE_URL": backend_origin,
        "PIN_REPOSITORY_ID": "1152335815",
        "PIN_REPOSITORY_NAME": "neurostackinfra/neurostack-api",
        "PIN_WORKFLOW_PATH": ".github/workflows/asaas-build-attestation.yml",
        "PIN_WORKFLOW_REF": "refs/heads/main",
        "PIN_KMS_KEY_ARN": (
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        "PIN_BUILD_AUDIENCE": "billing-api:producer-build-attestation",
        "PIN_ISSUER": "asaas-build-test-issuer",
        "PIN_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "PIN_OIDC_SUBJECT": (
            "repo:neurostackinfra@340698182/neurostack-api@1152335815:"
            "environment:asaas-build:ref:refs/heads/main:workflow_ref:"
            "neurostackinfra/neurostack-api/.github/workflows/"
            "asaas-build-attestation.yml@refs/heads/main"
        ),
        "BUILD_PUBLIC_KEY": public_key_pem,
    }
    backend_trust_readback = {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": "v1",
        "environment": "staging",
        "type": "billing_build_attestation.v1",
        "purpose": "billing-build-attestation.v1",
        "algorithm": "ECDSA_SHA_256",
        "issuer": backend_trust_env["PIN_ISSUER"],
        "kid": backend_trust_env["PIN_KMS_KEY_ARN"],
        "audience": backend_trust_env["PIN_BUILD_AUDIENCE"],
        "public_key_fingerprint_sha256": public_key_fingerprint,
        "repository_id": backend_trust_env["PIN_REPOSITORY_ID"],
        "repository_name": backend_trust_env["PIN_REPOSITORY_NAME"],
        "workflow_path": backend_trust_env["PIN_WORKFLOW_PATH"],
        "workflow_ref": backend_trust_env["PIN_WORKFLOW_REF"],
        "oidc_issuer": backend_trust_env["PIN_OIDC_ISSUER"],
        "oidc_subject": backend_trust_env["PIN_OIDC_SUBJECT"],
    }
    _validated, backend_trust_digest = (
        verifier._PREFLIGHT.validate_backend_trust_readback(
            backend_trust_env, backend_trust_readback
        )
    )
    evidence_digest = hashlib.sha256(_VALID_EVIDENCE_PNG).hexdigest()
    comment = " ".join(
        (
            "ASAAS-ADMIN-UI-ATTESTATION/2",
            f"run_id={run_id}",
            f"sha={sha}",
            "ref=refs/heads/main",
            "workflow_path=.github/workflows/asaas-build-attestation.yml",
            "environment=asaas-build",
            "reviewer_id=42",
            "reviewer_login=felps-json",
            f"preflight_sha256={preflight_digest}",
            "ruleset_bypass=empty",
            "environment_admin_bypass=disabled",
            "branch_policy=main-only",
            f"evidence_ref={_EVIDENCE_URL}",
            f"evidence_sha256={evidence_digest}",
        )
    )
    repository = {
        "full_name": "neurostackinfra/neurostack-api",
        "id": 1152335815,
        "owner": {"id": 340698182},
        "default_branch": "main",
    }
    run = {
        "id": int(run_id),
        "event": "workflow_dispatch",
        "status": "in_progress",
        "head_sha": sha,
        "head_branch": "main",
        "path": ".github/workflows/asaas-build-attestation.yml@refs/heads/main",
        "repository": {
            "full_name": "neurostackinfra/neurostack-api",
            "id": 1152335815,
        },
        "head_repository": {"id": 1152335815},
        "actor": {"id": 7, "login": "lorenzo"},
        "triggering_actor": {"id": 7, "login": "lorenzo"},
    }
    approval = {
        "state": "approved",
        "comment": comment,
        "environments": [{"name": "asaas-build"}],
        "user": {"id": 42, "login": "felps-json"},
    }
    api = {
        "https://api.github.com/repos/neurostackinfra/neurostack-api": repository,
        f"https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/{run_id}": run,
        f"https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/{run_id}/approvals": [
            approval
        ],
        "https://api.github.com/repos/neurostackinfra/neurostack-api/collaborators/felps-json/permission": {
            "permission": "admin"
        },
        "https://api.github.com/repos/neurostackinfra/neurostack-api/rulesets/123": _ruleset(),
        "https://api.github.com/repos/neurostackinfra/neurostack-api/environments/asaas-build": _environment(),
        "https://api.github.com/repos/neurostackinfra/neurostack-api/environments/asaas-production-deployment": _environment(
            "asaas-production-deployment"
        ),
        "backend_trust_readback": backend_trust_readback,
    }
    env = {
        "GH_TOKEN": "masked",
        "GITHUB_RUN_ID": run_id,
        "GITHUB_SHA": sha,
        "GITHUB_REPOSITORY": "neurostackinfra/neurostack-api",
        "GITHUB_REPOSITORY_ID": "1152335815",
        "GITHUB_REPOSITORY_OWNER_ID": "340698182",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_WORKFLOW_REF": (
            "neurostackinfra/neurostack-api/"
            ".github/workflows/asaas-build-attestation.yml@refs/heads/main"
        ),
        "ASAAS_BUILD_TRUST_MODE": "normal",
        "ASAAS_PREFLIGHT_READBACK_SHA256": preflight_digest,
        "ASAAS_PREFLIGHT_BUILD_TRUST_MODE": "normal",
        "ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256": backend_trust_digest,
        "ASAAS_PREFLIGHT_BACKEND_TRUST_PROBE_SHA256": "",
        "ASAAS_PREFLIGHT_BACKEND_ORIGIN": backend_origin,
        "ASAAS_PREFLIGHT_OBSERVED_AT": (now - timedelta(minutes=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        **backend_trust_env,
    }

    def get_json(url: str, token: str):
        assert token == "masked"
        return api[url]

    def get_collection(url: str, token: str, *, list_field=None):
        assert token == "masked"
        if "/rulesets?" in url:
            return [{"id": 123, "name": "asaas-producer-release-main"}]
        if "deployment-branch-policies" in url:
            return [
                {
                    "id": 12345,
                    "node_id": "BEP_kwDOABC123",
                    "name": "main",
                    "type": "branch",
                }
            ]
        assert list_field == "jobs"
        return [
            {
                "name": "preflight",
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "Build and sign after live protections and environment approval",
                "head_sha": sha,
                "status": "in_progress",
                "started_at": "2026-10-16T04:59:45Z",
            },
        ]

    def get_evidence(url: str) -> bytes:
        assert url == _EVIDENCE_URL
        return _VALID_EVIDENCE_PNG

    def fetch_backend_trust(environment, *, expected_digest=None):
        api.setdefault("backend_trust_fetch_calls", []).append(expected_digest)
        assert (
            verifier._PREFLIGHT.backend_trust_origin(environment)
            == environment["ASAAS_PREFLIGHT_BACKEND_ORIGIN"]
        )
        return verifier._PREFLIGHT.validate_backend_trust_readback(
            environment,
            api["backend_trust_readback"],
            expected_digest=expected_digest,
        )

    verifier._PREFLIGHT.fetch_backend_trust_readback = fetch_backend_trust

    _, digest_info = verifier._controls_snapshot(
        "masked",
        get_json=get_json,
        get_collection=get_collection,
        backend_trust_digest=backend_trust_digest,
        backend_trust_origin=backend_origin,
    )
    preflight_digest = digest_info["digest"]
    api[
        f"https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/{run_id}/approvals"
    ][0]["comment"] = comment.replace(
        "preflight_sha256=" + "c" * 64, f"preflight_sha256={preflight_digest}"
    )
    env["ASAAS_PREFLIGHT_READBACK_SHA256"] = preflight_digest

    return verifier, env, api, get_json, get_collection, get_evidence, now


def _bootstrap_approval_gate_fixture(now: datetime):
    verifier, env, api, get_json, get_collection, get_evidence, now = (
        _approval_gate_fixture(now)
    )
    operation_id = "89bf1556-9f6d-4f39-b5c8-8dcb041877da"
    run_id = env["GITHUB_RUN_ID"]
    run_path = (
        "https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/"
        f"{run_id}"
    )
    api[run_path]["display_title"] = f"ASAAS producer build bootstrap {operation_id}"
    env.update(
        {
            "INPUT_OPERATION_ID": operation_id,
            "GITHUB_RUN_ATTEMPT": "1",
            "ASAAS_BUILD_TRUST_MODE": "bootstrap",
            "ASAAS_PREFLIGHT_BUILD_TRUST_MODE": "bootstrap",
            "ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256": "",
            "ASAAS_PREFLIGHT_BACKEND_TRUST_PROBE_SHA256": "e" * 64,
        }
    )

    def get_bootstrap_collection(url: str, token: str, *, list_field=None):
        if list_field == "workflow_runs":
            return [
                {
                    "id": int(run_id),
                    "event": "workflow_dispatch",
                    "display_title": f"ASAAS producer build bootstrap {operation_id}",
                }
            ]
        return get_collection(url, token, list_field=list_field)

    old_preflight_digest = env["ASAAS_PREFLIGHT_READBACK_SHA256"]
    _, digest_info = verifier._controls_snapshot(
        "masked",
        get_json=get_json,
        get_collection=get_bootstrap_collection,
        backend_trust_digest="",
        backend_trust_origin=env["ASAAS_PREFLIGHT_BACKEND_ORIGIN"],
        trust_mode_value="bootstrap",
        backend_probe_digest=env["ASAAS_PREFLIGHT_BACKEND_TRUST_PROBE_SHA256"],
    )
    env["ASAAS_PREFLIGHT_READBACK_SHA256"] = digest_info["digest"]
    approvals_path = run_path + "/approvals"
    api[approvals_path][0]["comment"] = api[approvals_path][0]["comment"].replace(
        f"preflight_sha256={old_preflight_digest}",
        f"preflight_sha256={digest_info['digest']}",
    )
    return (
        verifier,
        env,
        api,
        get_json,
        get_bootstrap_collection,
        get_evidence,
        now,
    )


def test_environment_approval_requires_run_bound_independent_admin_and_readback():
    now = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)
    verifier, env, _api, get_json, get_collection, get_evidence, now = (
        _approval_gate_fixture(now)
    )

    evidence = verifier.verify_environment_approval(
        env,
        get_json=get_json,
        get_collection=get_collection,
        get_evidence=get_evidence,
        now=now,
    )

    assert evidence == {
        "schema": "asaas_admin_ui_approval.v1",
        "run_id": "7123456789",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
        "environment": "asaas-build",
        "reviewer_id": "42",
        "reviewer_login": "felps-json",
        "reviewer_permission": "admin",
        "preflight_sha256": env["ASAAS_PREFLIGHT_READBACK_SHA256"],
        "build_trust_mode": "normal",
        "backend_trust_readback_sha256": env["ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256"],
        "backend_trust_probe_sha256": "",
        "backend_trust_origin": env["ASAAS_PREFLIGHT_BACKEND_ORIGIN"],
        "evidence_ref": _EVIDENCE_URL,
        "evidence_sha256": hashlib.sha256(_VALID_EVIDENCE_PNG).hexdigest(),
        "evidence_verified": "true",
        "evidence_size_bytes": str(len(_VALID_EVIDENCE_PNG)),
        "approval_gate": "github_environment_required_reviewer_approved",
        "approval_gate_satisfied_at_utc": "2026-10-16T04:59:45Z",
        "approval_observed_at_utc": "2026-10-16T05:00:00Z",
    }
    assert _api["backend_trust_fetch_calls"] == [
        env["ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256"]
    ]


def test_bootstrap_approval_rechecks_exact_absence_before_oidc():
    now = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)
    verifier, env, _api, get_json, get_collection, get_evidence, now = (
        _bootstrap_approval_gate_fixture(now)
    )
    probes = []

    def absent(environment, *, expected_digest=None):
        probes.append(expected_digest)
        assert environment["ASAAS_PREFLIGHT_BACKEND_ORIGIN"] == (
            "https://staging.api.neurostack.global"
        )
        return expected_digest

    verifier._PREFLIGHT.probe_backend_trust_absent = absent
    evidence = verifier.verify_environment_approval(
        env,
        get_json=get_json,
        get_collection=get_collection,
        get_evidence=get_evidence,
        now=now,
    )
    assert evidence["build_trust_mode"] == "bootstrap"
    assert evidence["backend_trust_readback_sha256"] == ""
    assert evidence["backend_trust_probe_sha256"] == "e" * 64
    assert probes == ["e" * 64]


def test_bootstrap_approval_blocks_when_trust_appears_after_preflight():
    now = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)
    verifier, env, _api, get_json, get_collection, get_evidence, now = (
        _bootstrap_approval_gate_fixture(now)
    )

    def trust_appeared(*_args, **_kwargs):
        raise verifier._PREFLIGHT.GateError("bootstrap_trust_already_active")

    verifier._PREFLIGHT.probe_backend_trust_absent = trust_appeared
    with pytest.raises(
        verifier.ApprovalGateError, match="bootstrap_trust_already_active"
    ):
        verifier.verify_environment_approval(
            env,
            get_json=get_json,
            get_collection=get_collection,
            get_evidence=get_evidence,
            now=now,
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("approver", "environment_approval_reviewer_mismatch"),
        ("self_review", "environment_approval_not_independent"),
        ("non_admin", "environment_reviewer_is_not_repository_admin"),
        ("bypass_comment", "ruleset_bypass_not_attested"),
        ("evidence_url", "approval_evidence_ref_invalid"),
        ("evidence_digest", "approval_evidence_digest_mismatch"),
        ("self_asserted_time", "approval_comment_fields_invalid"),
        ("missing_actor", "actions_run_actor_missing"),
        (
            "missing_triggering_actor",
            "actions_run_triggering_actor_missing_or_invalid",
        ),
        ("changed_controls", "github_controls_changed_after_preflight"),
        ("changed_backend_trust", "backend_trust_pin_mismatch"),
        ("changed_backend_fingerprint", "backend_trust_pin_mismatch"),
        ("backend_trust_unavailable", "backend_trust_readback_unavailable"),
        (
            "backend_trust_origin_changed",
            "backend_trust_origin_changed_after_preflight",
        ),
    ],
)
def test_environment_approval_fails_closed_for_untrusted_evidence(
    mutation: str, reason: str
) -> None:
    now = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)
    verifier, env, api, get_json, get_collection, get_evidence, now = (
        _approval_gate_fixture(now)
    )
    approvals = api[
        "https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/7123456789/approvals"
    ]
    comment = approvals[0]["comment"]
    if mutation == "approver":
        approvals[0]["user"] = {"id": 99, "login": "other-user"}
    elif mutation == "self_review":
        api[
            "https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/7123456789"
        ]["actor"] = {"id": 42, "login": "felps-json"}
    elif mutation == "missing_actor":
        api[
            "https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/7123456789"
        ].pop("actor")
    elif mutation == "missing_triggering_actor":
        api[
            "https://api.github.com/repos/neurostackinfra/neurostack-api/actions/runs/7123456789"
        ].pop("triggering_actor")
    elif mutation == "non_admin":
        api[
            "https://api.github.com/repos/neurostackinfra/neurostack-api/collaborators/felps-json/permission"
        ] = {"permission": "write"}
    elif mutation == "bypass_comment":
        approvals[0]["comment"] = comment.replace(
            "ruleset_bypass=empty", "ruleset_bypass=present"
        )
    elif mutation == "evidence_url":
        approvals[0]["comment"] = comment.replace(
            _EVIDENCE_URL,
            "https://attacker.invalid/screenshot.png",
        )
    elif mutation == "evidence_digest":
        digest = hashlib.sha256(_VALID_EVIDENCE_PNG).hexdigest()
        approvals[0]["comment"] = comment.replace(
            f"evidence_sha256={digest}", f"evidence_sha256={'e' * 64}"
        )
    elif mutation == "self_asserted_time":
        approvals[0]["comment"] = comment + " reviewed_at_utc=2026-10-16T04:59:30Z"
    elif mutation == "changed_controls":
        api["https://api.github.com/repos/neurostackinfra/neurostack-api/rulesets/123"][
            "rules"
        ][1]["parameters"]["required_status_checks"][0][
            "context"
        ] = "Different required check"
    elif mutation == "changed_backend_trust":
        api["backend_trust_readback"]["issuer"] = "unexpected-issuer"
    elif mutation == "changed_backend_fingerprint":
        api["backend_trust_readback"]["public_key_fingerprint_sha256"] = "f" * 64
    elif mutation == "backend_trust_unavailable":

        def unavailable(*_args, **_kwargs):
            raise verifier._PREFLIGHT.GateError("backend_trust_readback_unavailable")

        verifier._PREFLIGHT.fetch_backend_trust_readback = unavailable
    elif mutation == "backend_trust_origin_changed":
        env["ASAAS_PREFLIGHT_BACKEND_ORIGIN"] = "https://attacker.invalid"

    with pytest.raises(verifier.ApprovalGateError, match=reason):
        verifier.verify_environment_approval(
            env,
            get_json=get_json,
            get_collection=get_collection,
            get_evidence=get_evidence,
            now=now,
        )


def test_github_attachment_fetch_verifies_png_and_restricts_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = _approval_module()

    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "image/png"

    class Response:
        status = 200
        headers = Headers()

        @staticmethod
        def geturl() -> str:
            return "https://private-user-images.githubusercontent.com/assets/signed.png?sig=redacted"

        @staticmethod
        def read(limit: int) -> bytes:
            assert limit == verifier.MAX_EVIDENCE_BYTES + 1
            return _VALID_EVIDENCE_PNG

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class Opener:
        @staticmethod
        def open(request, *, timeout: int):
            assert request.full_url == _EVIDENCE_URL
            assert timeout == 15
            return Response()

    monkeypatch.setattr(verifier, "build_opener", lambda _handler: Opener())
    assert verifier._fetch_github_attachment(_EVIDENCE_URL) == _VALID_EVIDENCE_PNG

    handler = verifier._GitHubAttachmentRedirect()
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_ref_invalid"
    ):
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "https://attacker.invalid/payload.png",
        )


def test_png_validation_checks_chunk_crc_and_decoded_data() -> None:
    verifier = _approval_module()
    idat_offset = len(verifier.PNG_SIGNATURE) + 25
    idat_length = struct.unpack_from(">I", _VALID_EVIDENCE_PNG, idat_offset)[0]
    data_start = idat_offset + 8
    data_end = data_start + idat_length

    bad_crc = bytearray(_VALID_EVIDENCE_PNG)
    bad_crc[data_end] ^= 1
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_crc_invalid"
    ):
        verifier._validate_png(bytes(bad_crc))

    bad_deflate = bytearray(_VALID_EVIDENCE_PNG)
    bad_deflate[data_start] = 0
    bad_deflate[data_end : data_end + 4] = zlib.crc32(
        b"IDAT" + bad_deflate[data_start:data_end]
    ).to_bytes(4, "big")
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_data_invalid"
    ):
        verifier._validate_png(bytes(bad_deflate))


def test_png_validation_rejects_idat_expansion_beyond_expected_raster_size():
    verifier = _approval_module()
    overexpanded_idat = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b"\0" + b"x" * 100_000))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_data_invalid"
    ):
        verifier._validate_png(overexpanded_idat)


def test_png_validation_rejects_reserved_chunk_name_bit_and_malformed_ancillary_chunks():
    verifier = _approval_module()
    invalid_reserved_bit = _png_before_idat(_png_chunk(b"abca", b"x"))
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(invalid_reserved_bit)

    malformed_gamma = _png_before_idat(_png_chunk(b"gAMA", b"\0\1"))
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(malformed_gamma)

    invalid_srgb_enum = _png_before_idat(_png_chunk(b"sRGB", b"\x04"))
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(invalid_srgb_enum)

    duplicate_phys = _png_before_idat(
        _png_chunk(b"pHYs", b"\0\0\0\1\0\0\0\1\0"),
        _png_chunk(b"pHYs", b"\0\0\0\1\0\0\0\1\0"),
    )
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(duplicate_phys)

    late_gamma = (
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + _png_chunk(b"PLTE", b"\0\0\0")
        + _png_chunk(b"gAMA", b"\0\0\xb1\x8f")
        + _png_chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0"))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(b"\x89PNG\r\n\x1a\n" + late_gamma)


def test_png_validation_checks_known_ancillary_chunks_and_indexed_palette_references():
    verifier = _approval_module()
    valid_ancillary = _png_before_idat(
        _png_chunk(b"gAMA", b"\0\0\xb1\x8f"),
        _png_chunk(b"sRGB", b"\x00"),
        _png_chunk(b"pHYs", b"\0\0\0\1\0\0\0\1\0"),
    )
    verifier._validate_png(valid_ancillary)

    verifier._validate_png(_indexed_png(0x80))
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_data_invalid"
    ):
        verifier._validate_png(_indexed_png(0x80, palette_entries=1))


def test_png_iccp_profile_uses_strict_bounded_zlib_stream():
    verifier = _approval_module()

    def png_with_profile(stream: bytes) -> bytes:
        return _png_before_idat(_png_chunk(b"iCCP", b"Profile\0\0" + stream))

    valid_stream = zlib.compress(b"P" * 128)
    verifier._validate_png(png_with_profile(valid_stream))

    oversized_stream = zlib.compress(b"P" * (verifier.MAX_DECODED_PNG_TEXT_BYTES + 1))
    truncated_stream = valid_stream[:-1]
    trailing_stream = valid_stream + b"trailing"
    for invalid_stream in (oversized_stream, truncated_stream, trailing_stream):
        with pytest.raises(
            verifier.ApprovalGateError, match="approval_evidence_png_invalid"
        ):
            verifier._validate_png(png_with_profile(invalid_stream))

    short_profile = zlib.compress(b"P" * 127)
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(png_with_profile(short_profile))


@pytest.mark.parametrize("chunk_type", [b"zTXt", b"iTXt"])
def test_png_validation_requires_complete_single_zlib_text_stream(chunk_type: bytes):
    verifier = _approval_module()

    def png_with_text(stream: bytes) -> bytes:
        prefix = (
            b"Comment\0\0" if chunk_type == b"zTXt" else b"Comment\0\x01\0en\0Comment\0"
        )
        return _png_before_idat(_png_chunk(chunk_type, prefix + stream))

    valid_stream = zlib.compress(b"approval metadata")
    verifier._validate_png(png_with_text(valid_stream))

    corrupt_stream = bytearray(valid_stream)
    corrupt_stream[-1] ^= 1
    invalid_streams = (
        valid_stream[:-1],
        valid_stream + b"trailing",
        valid_stream + zlib.compress(b"second member"),
        bytes(corrupt_stream),
    )
    for invalid_stream in invalid_streams:
        with pytest.raises(
            verifier.ApprovalGateError, match="approval_evidence_png_invalid"
        ):
            verifier._validate_png(png_with_text(invalid_stream))


def test_png_text_chunks_reject_nul_in_text_fields():
    verifier = _approval_module()
    verifier._validate_png(_png_before_idat(_png_chunk(b"tEXt", b"Comment\0caf\xe9")))
    text_chunks = (
        _png_chunk(b"tEXt", b"Comment\0hello\0hidden"),
        _png_chunk(b"zTXt", b"Comment\0\0" + zlib.compress(b"hello\0hidden")),
    )
    for text_chunk in text_chunks:
        with pytest.raises(
            verifier.ApprovalGateError, match="approval_evidence_png_invalid"
        ):
            verifier._validate_png(_png_before_idat(text_chunk))


@pytest.mark.parametrize("compressed", [False, True])
def test_png_itxt_validates_utf8_and_nul_for_translated_keyword_and_text(
    compressed: bool,
):
    verifier = _approval_module()

    def png_with_itxt(translated_keyword: bytes, text: bytes) -> bytes:
        compression_flag = int(compressed)
        text_payload = zlib.compress(text) if compressed else text
        data = (
            b"Comment\0"
            + bytes((compression_flag, 0))
            + b"en\0"
            + translated_keyword
            + b"\0"
            + text_payload
        )
        return _png_before_idat(_png_chunk(b"iTXt", data))

    verifier._validate_png(png_with_itxt("Comentario".encode(), "ação válida".encode()))

    invalid_values = (
        (b"\xff", b"valid text"),
        (b"translated", b"\xff"),
        (b"translated", b"text\0with nul"),
    )
    for translated_keyword, text in invalid_values:
        with pytest.raises(
            verifier.ApprovalGateError, match="approval_evidence_png_invalid"
        ):
            verifier._validate_png(png_with_itxt(translated_keyword, text))


def test_png_text_expansion_budget_is_strict_and_cumulative():
    verifier = _approval_module()
    budget = verifier.MAX_DECODED_PNG_TEXT_BYTES
    keyword = b"Comment"
    exact_limit_text = b"x" * (budget - len(keyword))
    verifier._validate_png(
        _png_before_idat(_png_chunk(b"tEXt", keyword + b"\0" + exact_limit_text))
    )

    oversized_text = zlib.compress(b"x" * (budget + 1))
    oversized_chunk = _png_chunk(b"zTXt", keyword + b"\0\0" + oversized_text)
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(_png_before_idat(oversized_chunk))

    per_chunk_text = b"y" * (budget // 2)
    repeated_chunks = (
        _png_chunk(b"zTXt", keyword + b"\0\0" + zlib.compress(per_chunk_text)),
        _png_chunk(
            b"iTXt", b"Other\0\x01\0en\0Title\0" + zlib.compress(per_chunk_text)
        ),
    )
    with pytest.raises(
        verifier.ApprovalGateError, match="approval_evidence_png_invalid"
    ):
        verifier._validate_png(_png_before_idat(*repeated_chunks))


def test_attestation_workflow_gates_all_aws_and_push_steps_after_live_preflight():
    source, workflow = _workflow()
    jobs = workflow["jobs"]
    preflight = jobs["preflight"]
    build = jobs["build-attestation"]
    preflight_step = next(
        step for step in preflight["steps"] if step.get("id") == "gate"
    )
    gate_run = preflight_step["run"]
    preflight_script = _PREFLIGHT_SCRIPT.read_text(encoding="utf-8")
    signer_script = (
        _PREFLIGHT_SCRIPT.parents[0] / "sign_asaas_build_attestation.py"
    ).read_text(encoding="utf-8")
    envelope_script = (
        _PREFLIGHT_SCRIPT.parents[1] / "src" / "services" / "billing_signed_envelope.py"
    ).read_text(encoding="utf-8")

    assert workflow["permissions"] == {}
    assert "run-name: ASAAS producer build ${{ inputs.trust_mode }}" in source
    assert "      trust_mode:" in source
    assert "        default: normal" in source
    assert "          - bootstrap" in source
    assert "cancel-in-progress: false" in source
    assert "group: asaas-producer-operation-${{ inputs.operation_id }}" in source
    assert "bootstrap_backend_origin_pin_mismatch" in preflight_script
    assert "bootstrap_operation_already_used" in preflight_script
    assert "bootstrap_run_attempt_must_be_first" in preflight_script
    assert set(jobs) == {"preflight", "build-attestation"}
    assert preflight["permissions"] == {
        "actions": "read",
        "administration": "read",
        "contents": "read",
    }
    assert "id-token" not in preflight["permissions"]
    assert "INPUT_TRUST_MODE" in preflight_step["env"]
    assert "aws-actions/configure-aws-credentials" not in gate_run
    assert "docker buildx build --push" not in gate_run
    assert gate_run == "python3 scripts/asaas_build_release_preflight.py"
    assert 'environment.get("can_admins_bypass") is False' in preflight_script
    assert "deployment-branch-policies" in preflight_script
    preflight_main = preflight_script.split("def main():", 1)[1]
    assert (
        preflight_main.index("validate_current_run(token, env)")
        < preflight_main.index("write_admin_ui_approval_instructions(")
        < preflight_script.index('output.write("rollout_scope=validated')
    )
    assert "require_run_bound_admin_ui_evidence" not in preflight_script
    assert "preflight_readback_sha256" in preflight_script
    assert "ASAAS_PRODUCER_OPERATIONAL_AUTHORIZATION_REFERENCE" not in source
    assert _gate_namespace()["OPERATIONAL_AUTHORIZATION_REFERENCE"] == _REFERENCE

    assert build["needs"] == "preflight"
    assert "needs.preflight.outputs.rollout_scope == 'validated'" in build["if"]
    assert build["environment"] == "asaas-build"
    assert build["permissions"] == {
        "actions": "read",
        "administration": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert "id-token: write" not in source.split("jobs:", 1)[0]
    assert source.count("id-token: write") == 1
    action_steps = [
        step for job in jobs.values() for step in job["steps"] if "uses" in step
    ]
    assert action_steps
    assert {step["uses"] for step in action_steps} == _EXPECTED_ACTION_PINS

    steps = build["steps"]
    setup_python = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Set up pinned CPython 3.12"
    )
    verify_approval = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Verify run-bound administrator UI approval before OIDC"
    )
    clean_source = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Verify clean protected source and hash the build bundle"
    )
    install = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install signing dependencies"
    )
    pins = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Validate signer pins before requesting OIDC credentials"
    )
    credentials = next(
        index
        for index, step in enumerate(steps)
        if "aws-actions/configure-aws-credentials@" in step.get("uses", "")
    )
    push = next(
        index
        for index, step in enumerate(steps)
        if "docker buildx build --push" in step.get("run", "")
    )
    assert (
        setup_python
        < verify_approval
        < clean_source
        < install
        < pins
        < credentials
        < push
    )
    assert "ASAAS_PREFLIGHT_READBACK_SHA256" in steps[verify_approval]["env"]
    approval_env = steps[verify_approval]["env"]
    assert (
        approval_env["ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256"]
        == "${{ needs.preflight.outputs.backend_trust_readback_sha256 }}"
    )
    assert (
        approval_env["ASAAS_PREFLIGHT_BACKEND_ORIGIN"]
        == "${{ needs.preflight.outputs.backend_trust_origin }}"
    )
    assert "PIN_BACKEND_BASE_URL" in approval_env
    assert "verify_asaas_build_environment_approval.py" in steps[verify_approval]["run"]
    assert (
        steps[pins]["env"]["ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256"]
        == "${{ needs.preflight.outputs.backend_trust_readback_sha256 }}"
    )
    assert "PIN_BACKEND_BASE_URL" in steps[pins]["env"]
    assert "id-token" not in workflow["jobs"]["preflight"]["permissions"]
    assert "id-token" in build["permissions"]
    assert (
        "git status --porcelain=v1 --untracked-files=all" in steps[clean_source]["run"]
    )
    assert "git archive --format=tar" in steps[clean_source]["run"]
    assert (
        steps[setup_python]["uses"]
        == "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
    )
    assert steps[setup_python]["with"]["python-version"] == "3.12"
    assert "asaas-rollout-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in steps[push]["run"]
    assert "group7b-" not in source
    assert "aws ecs" not in source.lower()
    assert "kms:GetPublicKey" not in source
    assert set(re.findall(r"(?m)^\s+aws\s+([a-z-]+)", source)) == {"ecr"}
    assert re.search(r'boto3\.client\(\s*"kms"', signer_script)
    assert "self._kms.sign(" in envelope_script
    assert "get_public_key" not in signer_script.lower() + envelope_script.lower()
    assert "BILLING_PRODUCER_BUILD_ROLE_ARN" in source
    assert "register-build-attestation" not in source
    assert "billing_build_attestation.v1" not in source
    assert "refund payment" not in source.lower()


def test_build_trust_bootstrap_runbook_matches_approved_group_sequence_and_controls():
    runbook = (
        _WORKFLOW.parents[2]
        / "docs"
        / "billing"
        / "asaas-producer-build-trust-bootstrap.md"
    ).read_text(encoding="utf-8")
    normalized = " ".join(runbook.split())
    group4 = runbook.index("Group 4 publishes and signs only")
    group5 = runbook.index("## Group 5 one-time deployment bootstrap")
    later = runbook.index("## Later deployments")

    assert "eb217bef741981537e6b6277242e0fb7fccd05b3e8b5e216768d7c7926d8ccb8" in runbook
    assert "It never deploys or registers the attestation" in normalized
    assert group4 < group5 < later
    assert "exactly one protected deployment" in normalized
    assert (
        "asaas-producer-image-rollout/WISH.md#2026-10-15-separate-build-and-rollout-plan-approved"
        in normalized
    )
    assert "before backend registration" in normalized
    assert "Immediately after successful healthy readbacks" in normalized
    assert "byte-identical envelope" in normalized
    assert "before its protected deployment approval" in normalized
    assert "Felps (`felps-json`) remains the sole independent PR reviewer" in normalized
    assert (
        "Felps (`felps-json`) remains the sole independent PR reviewer and the current "
        "protected-environment reviewer required by live settings" in normalized
    )
    assert "Lorenzo performs final PR approval and merge" in normalized
    assert "do not change settings to bypass last-push review" in normalized
    assert (
        "A distinct preallocated deployment operation UUID binds the run to the exact "
        "Group 4 artifact tuple: attestation ID, immutable image digest, build run ID, "
        "and signed-envelope SHA-256" in normalized
    )
    assert (
        "This operation ID is deployment metadata; it is not added to the signed envelope"
        in normalized
    )
    assert (
        "Pending or rejected-before-start environment records do not consume the slot"
        in normalized
    )
    assert (
        "once the protected job starts, any failure or cancellation consumes it"
        in normalized
    )
    assert "No retry or second unregistered bootstrap is allowed" in normalized
    assert "no user JWT" in normalized
    assert "PostgreSQL console/session" in normalized
    assert (
        "If either is absent, withhold approval; do not substitute ad hoc SQL"
        in normalized
    )
    assert "`pg_stat_activity`" in normalized
    assert "Never call `GetSecretValue`" in normalized
    assert "at least 60 seconds" in normalized
    assert "`createdAt`" in normalized and "`startedAt`" in normalized
    assert "pre-approved rollback path" in normalized
    assert "ASAAS/credit egress and commercial top-up disabled" in normalized
    assert "before, during, and after the bootstrap" in normalized


def test_attestation_workflow_installs_signer_from_hash_locked_requirements():
    workflow, document = _workflow()
    build = document["jobs"]["build-attestation"]
    install = next(
        step
        for step in build["steps"]
        if step.get("name") == "Install signing dependencies"
    )
    assert "--require-hashes" in install["run"]
    assert "-r scripts/requirements-asaas-build-attestation.txt" in install["run"]
    lock = _WORKFLOW.parents[2] / "scripts" / "requirements-asaas-build-attestation.txt"
    lock_text = lock.read_text(encoding="utf-8")
    assert lock_text.count("--hash=sha256:") == 10
    requirement_lines = [
        line
        for line in lock_text.splitlines()
        if line and not line.startswith(("#", " "))
    ]
    assert all("==" in line for line in requirement_lines)
    assert ">=" not in lock_text


def test_attestation_workflow_is_not_ignored_by_git() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(_WORKFLOW)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
