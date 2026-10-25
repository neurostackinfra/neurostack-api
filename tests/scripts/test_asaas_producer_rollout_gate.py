from __future__ import annotations

import hashlib
import json
import struct
import zlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts import asaas_producer_rollout_gate as rollout_gate
from scripts.asaas_oidc_ui_freshness import validate_ui_evidence_freshness
from scripts.asaas_producer_rollout_gate import (
    DEPLOY_ROLE_VARIABLE,
    GateError,
    _require_distinct_ui_approval_screenshots,
    _validate_ui_approval_comment,
    validate_canonical_registration_comment,
    validate_deploy_role_environment_variable,
    validate_deployment_history,
    validate_repository_protections,
    validate_runtime_deploy_role_variable,
)

REPOSITORY = "neurostackinfra/neurostack-api"
WORKFLOW = ".github/workflows/asaas-producer-image-deploy.yml"
DEPLOY_ROLE_ARN = rollout_gate.PINNED_DEPLOY_ROLE_ARN


@pytest.mark.parametrize(
    "value",
    [
        {"name": "OTHER_ROLE", "value": DEPLOY_ROLE_ARN},
        {"name": DEPLOY_ROLE_VARIABLE, "value": "arn:aws:iam::388460142431:role/other"},
        {
            "name": DEPLOY_ROLE_VARIABLE,
            "value": "arn:aws:iam::388460142431:role/neurostack-asaas-producer-release-DeployRole-AbCd1234",
        },
        {"name": DEPLOY_ROLE_VARIABLE, "value": ""},
        None,
    ],
)
def test_deploy_role_readback_rejects_wrong_name_or_unpinned_arn(value) -> None:
    with pytest.raises(GateError, match="protected_deploy_role_readback_invalid"):
        validate_deploy_role_environment_variable(value)


def test_runtime_deploy_role_must_equal_hashed_protected_value() -> None:
    assert (
        validate_runtime_deploy_role_variable(DEPLOY_ROLE_ARN, DEPLOY_ROLE_ARN)
        == DEPLOY_ROLE_ARN
    )
    with pytest.raises(GateError, match="protected_deploy_role_variable_mismatch"):
        validate_runtime_deploy_role_variable(
            "arn:aws:iam::388460142431:role/arbitrary", DEPLOY_ROLE_ARN
        )


def test_repository_protection_readback_hashes_exact_group2_deploy_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    role_variable_url = (
        "https://api.github.com/repos/neurostackinfra/neurostack-api/"
        "environments/asaas-production-deployment/variables/"
        "BILLING_PRODUCER_DEPLOY_ROLE_ARN"
    )
    seen: list[str] = []

    def get(url: str, token: str):
        seen.append(url)
        if url.endswith("/repos/neurostackinfra/neurostack-api"):
            return (
                {
                    "full_name": REPOSITORY,
                    "id": rollout_gate.REPOSITORY_ID,
                    "owner": {"id": rollout_gate.OWNER_ID},
                    "default_branch": "main",
                },
                None,
            )
        if url == role_variable_url:
            return (
                {"name": DEPLOY_ROLE_VARIABLE, "value": DEPLOY_ROLE_ARN},
                None,
            )
        raise AssertionError("unexpected GitHub readback endpoint")

    monkeypatch.setattr(
        rollout_gate.github_preflight,
        "validate_ruleset",
        lambda *args, **kwargs: {"ruleset_id": 42},
    )
    monkeypatch.setattr(
        rollout_gate.github_preflight,
        "validate_environment",
        lambda name, *args, **kwargs: {"environment": name},
    )
    protections = validate_repository_protections(
        "read-only-token",
        github_get=get,
        github_collection=lambda *args, **kwargs: [],
    )
    assert role_variable_url in seen
    assert protections["deploy_role_variable"] == DEPLOY_ROLE_VARIABLE
    assert protections["deploy_role_arn"] == DEPLOY_ROLE_ARN
    assert protections["ruleset_bypass_api_observation"] == "omitted"
    assert protections["deploy_role_output_pin"] == {
        "cloudformation_stack": "neurostack-asaas-producer-release",
        "output_key": "DeployRoleArn",
        "read_at_utc": "2026-10-16T23:02:35.755467Z",
        "readback_sha256": "765b3174d6a1b28205f0185f535fc6cdf2fa8b369c8af6e7583524aa437d7fb9",
    }

    changed = {
        **protections,
        "deploy_role_arn": DEPLOY_ROLE_ARN + "x",
    }
    original_hash = hashlib.sha256(rollout_gate._canonical(protections)).digest()
    changed_hash = hashlib.sha256(rollout_gate._canonical(changed)).digest()
    assert original_hash != changed_hash


def _png(rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + rgba))
        + chunk(b"IEND", b"")
    )


_PNG_1 = _png(b"\x00\x00\x00\xff")
_PNG_2 = _png(b"\xff\xff\xff\xff")


def _history_run(
    run_id: int, *, mode: str = "bootstrap", started: str | None = None
) -> dict[str, object]:
    operation = str(uuid4())
    attestation = str(uuid4())
    title = (
        f"ASAAS producer image {mode} operation={operation} build_run=123 "
        f"attestation={attestation} digest=sha256:{'a' * 64} envelope={'b' * 64}"
    )
    run = {
        "id": run_id,
        "display_title": title,
        "event": "workflow_dispatch",
        "path": f"{WORKFLOW}@refs/heads/main",
        "head_branch": "main",
        "head_sha": "c" * 40,
    }
    job = {"name": "deploy", "head_sha": run["head_sha"], "started_at": started}
    return {"run": run, "job": job}


def test_bootstrap_history_allows_pending_but_consumes_started_deploy_job() -> None:
    current = {"id": 200, "display_title": "current"}
    pending = _history_run(100, started=None)

    def collect(url: str, token: str, *, list_field: str):
        if "/runs?event=" in url:
            return [current, pending["run"]]
        return [pending["job"]]

    result = validate_deployment_history(
        "test-token",
        current_run_id="200",
        operation_id=str(uuid4()),
        mode="bootstrap",
        github_collection=collect,
    )
    assert result["bootstrap_slot"] == "available"
    pending["job"]["started_at"] = "2026-10-17T00:00:00Z"
    with pytest.raises(GateError, match="bootstrap_slot_already_consumed"):
        validate_deployment_history(
            "test-token",
            current_run_id="200",
            operation_id=str(uuid4()),
            mode="bootstrap",
            github_collection=collect,
        )


def test_canonical_registration_is_bound_to_run_operation_digest_and_admin_author() -> (
    None
):
    run_id = "200"
    operation_id = str(uuid4())
    attestation_id = str(uuid4())
    attestation_digest = "a" * 64
    registration = {
        "schema_version": "asaas_producer_registration.v1",
        "run_id": run_id,
        "operation_id": operation_id,
        "attestation_id": attestation_id,
        "attestation_digest": attestation_digest,
        "registered_at": "2026-10-17T00:00:00.000000Z",
        "registrar_login": "operator-admin",
        "registrar_role": "super_admin",
    }
    comment = {
        "body": "<!-- asaas-producer-registration.v1\n"
        + json.dumps(registration)
        + "\n-->",
        "user": {"login": "operator-admin"},
    }
    result = validate_canonical_registration_comment(
        [comment],
        token="test-token",
        run_id=run_id,
        operation_id=operation_id,
        attestation={
            "attestation_id": attestation_id,
            "attestation_digest": attestation_digest,
            "signed_envelope_sha256": attestation_digest,
        },
        github_get=lambda url, token: ({"permission": "admin"}, None),
    )
    assert result["registration_attestation_id"] == attestation_id
    registration["registrar_login"] = "someone-else"
    comment["body"] = (
        "<!-- asaas-producer-registration.v1\n" + json.dumps(registration) + "\n-->"
    )
    with pytest.raises(
        GateError, match="canonical_registration_registrar_author_mismatch"
    ):
        validate_canonical_registration_comment(
            [comment],
            token="test-token",
            run_id=run_id,
            operation_id=operation_id,
            attestation={
                "attestation_id": attestation_id,
                "attestation_digest": attestation_digest,
                "signed_envelope_sha256": attestation_digest,
            },
            github_get=lambda url, token: ({"permission": "admin"}, None),
        )


def test_ui_attestation_hashes_live_pngs_and_binds_fresh_run_review() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    refs = {
        "ruleset": "https://github.com/user-attachments/assets/12345678-1234-4abc-8def-0123456789ab",
        "deployment_environment": "https://github.com/user-attachments/assets/22345678-1234-4abc-8def-0123456789ab",
    }
    pngs = {refs["ruleset"]: _PNG_1, refs["deployment_environment"]: _PNG_2}
    comment = {
        "schema_version": "asaas_producer_deployment_ui_attestation.v1",
        "run_id": "200",
        "run_attempt": "1",
        "sha": "c" * 40,
        "ref": "refs/heads/main",
        "workflow_path": WORKFLOW,
        "environment": "asaas-production-deployment",
        "reviewer_login": "operator-admin",
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "ruleset_bypass_empty": True,
        "environment_admin_bypass_disabled": True,
        "sole_branch_policy": "main",
        "screenshots_sha256": {
            label: {
                "ref": reference,
                "sha256": hashlib.sha256(pngs[reference]).hexdigest(),
            }
            for label, reference in refs.items()
        },
        "task_definition_template_sha256": "d" * 64,
    }
    result = _validate_ui_approval_comment(
        json.dumps(comment),
        run={
            "run_id": "200",
            "run_attempt": "1",
            "sha": "c" * 40,
            "ref": "refs/heads/main",
            "workflow_path": WORKFLOW,
        },
        reviewer_login="operator-admin",
        preflight_observed_at=now - timedelta(minutes=1),
        approval_job_started_at=now + timedelta(minutes=1),
        template_sha256="d" * 64,
        screenshot_fetcher=lambda reference: pngs[reference],
    )
    assert result["checked_at_utc"] == comment["checked_at_utc"]
    assert set(result["screenshots"]) == {"ruleset", "deployment_environment"}
    assert set(result["screenshots"]["ruleset"]) == {"ref_sha256", "sha256"}
    assert "https://github.com" not in json.dumps(result)
    with pytest.raises(GateError, match="ui_approval_screenshots_reused_between_gates"):
        _require_distinct_ui_approval_screenshots(result, result)


def test_deployment_approval_phases_reject_reused_screenshot_refs_or_bytes() -> None:
    first = {
        "screenshots": {
            "ruleset": {"ref_sha256": "a" * 64, "sha256": "1" * 64},
            "environment": {"ref_sha256": "b" * 64, "sha256": "2" * 64},
        }
    }
    distinct = {
        "screenshots": {
            "ruleset": {"ref_sha256": "c" * 64, "sha256": "3" * 64},
            "environment": {"ref_sha256": "d" * 64, "sha256": "4" * 64},
        }
    }
    _require_distinct_ui_approval_screenshots(first, distinct)
    reused_ref = {
        "screenshots": {
            "ruleset": {"ref_sha256": "a" * 64, "sha256": "5" * 64},
            "environment": {"ref_sha256": "e" * 64, "sha256": "6" * 64},
        }
    }
    with pytest.raises(GateError, match="ui_approval_screenshots_reused_between_gates"):
        _require_distinct_ui_approval_screenshots(first, reused_ref)
    reused_bytes = {
        "screenshots": {
            "ruleset": {"ref_sha256": "f" * 64, "sha256": "1" * 64},
            "environment": {"ref_sha256": "g" * 64, "sha256": "7" * 64},
        }
    }
    with pytest.raises(GateError, match="ui_approval_screenshots_reused_between_gates"):
        _require_distinct_ui_approval_screenshots(first, reused_bytes)


@pytest.mark.parametrize(
    ("checked_at", "expected_error"),
    [
        ("", "ui_evidence_checked_at_missing_or_invalid"),
        ("2026-10-17T10:01:00.000000Z", "ui_evidence_stale_at_oidc_boundary"),
        ("2026-10-17T10:00:59.000000Z", "ui_evidence_stale_at_oidc_boundary"),
        ("2026-10-17T09:59:59.000000Z", "ui_evidence_stale_at_oidc_boundary"),
        ("2026-10-17T10:05:01.000000Z", "ui_evidence_checked_at_in_future"),
    ],
)
def test_oidc_boundary_freshness_rejects_missing_stale_and_future_evidence(
    checked_at: str, expected_error: str
) -> None:
    now = datetime(2026, 10, 17, 10, 5, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match=expected_error):
        validate_ui_evidence_freshness(checked_at, now=now)


def test_oidc_boundary_freshness_accepts_just_under_four_minutes() -> None:
    now = datetime(2026, 10, 17, 10, 5, tzinfo=timezone.utc)
    validate_ui_evidence_freshness("2026-10-17T10:01:01.000000Z", now=now)


def test_ui_attestation_rejects_stale_controls_and_unverified_screenshot_bytes() -> (
    None
):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    reference = "https://github.com/user-attachments/assets/12345678-1234-4abc-8def-0123456789ab"
    comment = {
        "schema_version": "asaas_producer_deployment_ui_attestation.v1",
        "run_id": "200",
        "run_attempt": "1",
        "sha": "c" * 40,
        "ref": "refs/heads/main",
        "workflow_path": WORKFLOW,
        "environment": "asaas-production-deployment",
        "reviewer_login": "operator-admin",
        "checked_at_utc": now.isoformat().replace("+00:00", "Z"),
        "ruleset_bypass_empty": True,
        "environment_admin_bypass_disabled": False,
        "sole_branch_policy": "main",
        "screenshots_sha256": {
            "ruleset": {"ref": reference, "sha256": hashlib.sha256(_PNG_1).hexdigest()},
            "deployment_environment": {
                "ref": "https://github.com/user-attachments/assets/22345678-1234-4abc-8def-0123456789ab",
                "sha256": hashlib.sha256(_PNG_2).hexdigest(),
            },
        },
        "task_definition_template_sha256": "d" * 64,
    }
    args = {
        "run": {
            "run_id": "200",
            "run_attempt": "1",
            "sha": "c" * 40,
            "ref": "refs/heads/main",
            "workflow_path": WORKFLOW,
        },
        "reviewer_login": "operator-admin",
        "preflight_observed_at": now - timedelta(minutes=1),
        "approval_job_started_at": now + timedelta(minutes=1),
        "template_sha256": "d" * 64,
        "screenshot_fetcher": lambda reference: (
            _PNG_1 if reference.rsplit("/", 1)[-1].startswith("12345678-") else _PNG_2
        ),
    }
    with pytest.raises(GateError, match="ui_approval_comment_mismatch"):
        _validate_ui_approval_comment(json.dumps(comment), **args)
    comment["environment_admin_bypass_disabled"] = True
    comment["ruleset_bypass_empty"] = False
    with pytest.raises(GateError, match="ui_approval_comment_mismatch"):
        _validate_ui_approval_comment(json.dumps(comment), **args)
    comment["ruleset_bypass_empty"] = True
    args["preflight_observed_at"] = now - timedelta(minutes=10)
    comment["checked_at_utc"] = (
        (now - timedelta(minutes=6)).isoformat().replace("+00:00", "Z")
    )
    with pytest.raises(GateError, match="ui_approval_timestamp_stale"):
        _validate_ui_approval_comment(json.dumps(comment), **args)
