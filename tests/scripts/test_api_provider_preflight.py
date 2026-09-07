"""Focused contract tests for the API-provider operational preflight."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import math
import os
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/api_provider_preflight.py"
SPEC = importlib.util.spec_from_file_location("api_provider_preflight", SCRIPT)
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


def _key_pair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return (
        base64.b64encode(private_bytes).decode(),
        base64.b64encode(public_bytes).decode(),
    )


@pytest.fixture(autouse=True)
def isolated_signing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_PROVIDER_EVIDENCE_HMAC_KEY", raising=False)
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "development")
    monkeypatch.setenv("DEPLOYMENT_ID", "deploy-safe-42")
    monkeypatch.setenv("DEVELOPMENT_DEPLOYMENT_ID", "development-42")
    monkeypatch.setenv("PRODUCTION_DEPLOYMENT_ID", "production-42")
    for environment in ("LOCAL", "DEVELOPMENT", "PRODUCTION"):
        for prefix in (
            "API_PROVIDER_COLLECTOR",
            "API_PROVIDER_ATTESTATION",
            "API_PROVIDER_RECEIPT_WEBHOOK_SMOKE",
            "API_PROVIDER_RECEIPT_REDIS_REHEARSAL",
            "API_PROVIDER_RECEIPT_ROLLBACK_REHEARSAL",
        ):
            private, public = _key_pair()
            monkeypatch.setenv(f"{prefix}_{environment}_PRIVATE_KEY", private)
            monkeypatch.setenv(f"{prefix}_{environment}_PUBLIC_KEY", public)


def fixture_observations(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "postgresql": {
            "active_meta_tenants": 2,
            "active_zapi_tenants": 3,
            "meta_missing_app_secret": 0,
        },
        "redis": {
            "used_memory_peak_bytes": 100_000,
            "maxmemory_bytes": 10_000_000,
            "maxmemory_policy": "allkeys-lru",
            "evicted_keys": 0,
        },
        "message_volume": {"daily": list(range(1, 32)), "p95_claim_bytes": 128},
        "deployment": {"workers": 4},
        "capability": {
            "zapi_path_roundtrip_pass": True,
            "external_edge_redaction_capable": True,
        },
        "application_redaction_baseline": {
            "status": "expected-fail",
            "complete_redaction_pass": False,
            "leaks": ["application-log-raw-request-target"],
        },
        "approvals": {},
    }
    value.update(overrides)
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def runtime_text(*parts: str) -> str:
    return "".join(parts)


def collect_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: dict[str, Any],
    environment: str = "development",
) -> tuple[Path, int]:
    evidence = tmp_path / "private-evidence"
    fixture_path = tmp_path / "fixture.json"
    raw = evidence / "wave0.raw.json"
    write_json(fixture_path, fixture)
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("API_PROVIDER_PREFLIGHT_FIXTURE", str(fixture_path))
    monkeypatch.setenv("API_PROVIDER_PREFLIGHT_NOW", "2026-08-06T12:00:00Z")
    result = preflight.main(
        [
            "collect",
            "--phase",
            "wave0",
            "--environment",
            environment,
            "--deployment-id",
            "deploy-safe-42",
            "--window-days",
            "31",
            "--raw-output",
            str(raw),
        ]
    )
    monkeypatch.delenv("API_PROVIDER_PREFLIGHT_NOW", raising=False)
    return raw, result


def write_synthetic_live_evidence(
    raw: Path,
    *,
    now: "datetime | None" = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Build verifier input without exercising or pretending to collect live systems.

    ``now`` controls collected_at / window timestamps.  Pass the same value you
    monkeypatch into ``preflight._utc_now`` so the verifier and the evidence agree
    on "current time".  Defaults to the real wall-clock UTC time, which is correct
    for tests that call ``verify`` without overriding ``_utc_now`` (``verify`` uses
    ``allow_override=False`` and always reads real time).
    """
    from datetime import datetime, timedelta, timezone

    if now is None:
        now = datetime.now(timezone.utc)
    value = json.loads(raw.read_text(encoding="utf-8"))
    expected_sources = preflight.SOURCE_NAMES - (
        set()
        if value["aggregates"]["meta_missing_app_secret"]
        else {"change-management"}
    )
    # Refresh collected_at and window boundaries to match the caller's "now" so the
    # staleness check (now - collected_at <= MAX_EVIDENCE_AGE) passes in every test.
    start = now - timedelta(days=preflight.WINDOW_DAYS)
    value["collected_at"] = now.isoformat()
    value["window"]["end"] = now.isoformat()
    value["window"]["start"] = start.isoformat()
    value.update(
        representative=True,
        source_mode="live-read-only",
        source_inventory=sorted(expected_sources),
        sink_inventory=sorted(preflight.SINK_NAMES),
        provenance={
            "mode": "live-read-only",
            "environment": value["environment"],
            "deployment_id": value["deployment_id"],
        },
        source_receipts={
            "postgresql": "0" * 64,
            "redis": "1" * 64,
            "message_observability": "2" * 64,
            "observability": "3" * 64,
            "smoke_collect_input": "5" * 64,
        },
        webhook_smoke={
            "status": "passed_real_callback",
            "active_zapi_count": value["aggregates"]["active_zapi_tenants"],
            "readiness": True,
            "activation": True,
            "requires_fresh_smoke_before_activation": False,
            "collect_raw_digest": "5" * 64,
        },
    )
    value.update(overrides)
    preflight._sign_evidence(value)
    write_json(raw, value)
    os.chmod(raw, 0o600)
    return value


def test_collect_fixture_is_deterministic_redacted_and_never_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_connection() -> None:
        raise AssertionError("fixture collection attempted a live connection")

    monkeypatch.setattr(preflight, "_collect_postgres", unexpected_connection)
    monkeypatch.setattr(preflight, "_collect_redis", unexpected_connection)
    raw, result = collect_fixture(tmp_path, monkeypatch, fixture_observations())

    assert result == 0
    first = raw.read_bytes()
    assert stat.S_IMODE(raw.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(raw.stat().st_mode) == 0o600
    assert json.loads(first)["representative"] is False
    raw.unlink()
    raw_again, result_again = collect_fixture(
        tmp_path, monkeypatch, fixture_observations()
    )
    assert result_again == 0
    assert raw_again.read_bytes() == first
    serialized = first.decode("utf-8")
    assert "https://" not in serialized
    assert "patient" not in serialized


def test_collect_rejects_raw_evidence_inside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_path = tmp_path / "fixture.json"
    write_json(fixture_path, fixture_observations())
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("API_PROVIDER_PREFLIGHT_FIXTURE", str(fixture_path))
    result = preflight.main(
        [
            "collect",
            "--phase",
            "wave0",
            "--environment",
            "local",
            "--deployment-id",
            "local",
            "--window-days",
            "31",
            "--raw-output",
            str(preflight.REPOSITORY_ROOT / "forbidden.raw.json"),
        ]
    )
    assert result == 2
    assert not (preflight.REPOSITORY_ROOT / "forbidden.raw.json").exists()


def test_verify_go_requires_live_representative_evidence_and_keeps_expected_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw, result = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    assert result == 0
    write_synthetic_live_evidence(raw)
    attestation = tmp_path / "wave0-development.attestation.json"

    result = preflight.main(
        [
            "verify",
            "--phase",
            "wave0",
            "--input",
            str(raw),
            "--attestation",
            str(attestation),
        ]
    )

    assert result == 0
    assert "go: wave0 external preconditions are satisfied" in capsys.readouterr().out
    verified = json.loads(attestation.read_text(encoding="utf-8"))
    assert verified["decision"] == "go"
    baseline = verified["threshold_results"]["application_redaction_baseline"]
    assert baseline == {
        "leak_categories": ["application-log-raw-request-target"],
        "pass": False,
        "recorded": True,
        "status": "expected-fail",
    }
    serialized = attestation.read_text(encoding="utf-8").lower()
    for forbidden in ("phone_number", "tenant_id", "http://", "https://"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    ("mutation", "criterion"),
    [
        (
            lambda raw: (
                raw["capability"].update(zapi_path_roundtrip_pass=False),
                raw["webhook_smoke"].update(
                    status="failed_real_callback",
                    readiness=False,
                    activation=False,
                    requires_fresh_smoke_before_activation=True,
                ),
            ),
            "zapi_path_roundtrip",
        ),
        (
            lambda raw: (
                raw["capability"].update(external_edge_redaction_capable=False),
                raw["webhook_smoke"].update(
                    status="failed_real_callback",
                    readiness=False,
                    activation=False,
                    requires_fresh_smoke_before_activation=True,
                ),
            ),
            "external_edge_redaction",
        ),
        (lambda raw: raw["redis"].update(evicted_keys=1), "redis_evictions"),
        (lambda raw: raw.update(representative=False), "representative_evidence"),
    ],
)
def test_verify_no_go_names_exact_design_return_criterion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
    criterion: str,
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    value = write_synthetic_live_evidence(raw)
    mutation(value)
    preflight._sign_evidence(value)
    write_json(raw, value)
    attestation = tmp_path / "wave0-development.attestation.json"
    result = preflight.main(
        [
            "verify",
            "--phase",
            "wave0",
            "--input",
            str(raw),
            "--attestation",
            str(attestation),
        ]
    )
    assert result == 2
    assert criterion in json.loads(attestation.read_text())["design_return_criteria"]


def test_verify_uses_capacity_formula(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_observations()
    fixture["redis"]["maxmemory_bytes"] = 100_000
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture)
    value = write_synthetic_live_evidence(raw)
    preflight._sign_evidence(value)
    write_json(raw, value)
    attestation = tmp_path / "wave0-development.attestation.json"
    result = preflight.main(
        [
            "verify",
            "--phase",
            "wave0",
            "--input",
            str(raw),
            "--attestation",
            str(attestation),
        ]
    )
    assert result == 2
    assert (
        "redis_capacity"
        in json.loads(attestation.read_text())["design_return_criteria"]
    )


def test_meta_interruption_acceptance_requires_two_roles_and_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_observations()
    fixture["postgresql"]["meta_missing_app_secret"] = 2
    fixture["approvals"] = {
        "meta_interruption_acceptance": {
            "aggregate_count": 2,
            "product_owner_approved": True,
            "production_operations_owner_approved": True,
            "reference_id": "CHG-2042",
            "approved_at": "2026-08-06T09:00:00-03:00",
        }
    }
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture)
    value = write_synthetic_live_evidence(raw)
    preflight._sign_evidence(value)
    write_json(raw, value)
    attestation = tmp_path / "wave0-development.attestation.json"
    result = preflight.main(
        [
            "verify",
            "--phase",
            "wave0",
            "--input",
            str(raw),
            "--attestation",
            str(attestation),
        ]
    )
    assert result == 0
    approval = json.loads(attestation.read_text())["approvals"][
        "meta_interruption_acceptance"
    ]
    assert approval["reference_id"] == "CHG-2042"
    assert "name" not in json.dumps(approval).lower()


def test_scan_tracked_rejects_raw_and_capability_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    raw = tmp_path / "incident.raw.json"
    raw.write_text("{}", encoding="utf-8")
    capability = tmp_path / "notes.txt"
    capability_value = runtime_text("Actual", "Opaque", "Capability", "987654321")
    capability.write_text(
        f"https://edge.invalid/webhook/zapi/{capability_value}",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "incident.raw.json", "notes.txt"], cwd=tmp_path, check=True
    )
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1


def test_scan_tracked_accepts_real_verifier_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    write_synthetic_live_evidence(raw)
    attestation = tmp_path / "wave0-development.attestation.json"
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(attestation),
            ]
        )
        == 0
    )
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", attestation.name], cwd=tmp_path, check=True)
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 0


def test_cli_exposes_all_required_subcommands() -> None:
    parser = preflight._build_parser()
    help_text = parser.format_help()
    for command in (
        "collect",
        "verify",
        "scan-tracked",
        "redis-load",
        "webhook-smoke",
        "rollback-rehearsal",
        "release-decision",
    ):
        assert command in help_text


def test_cli_and_signing_contract_are_development_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8").lower()
    assert ("sta" + "ging") not in source
    assert "--development" in source
    assert "wave0-development.raw.json" in source
    assert "release-development.raw.json" in source
    assert (
        preflight._key_variable("collector", "collection", "development", private=True)
        == "API_PROVIDER_COLLECTOR_DEVELOPMENT_PRIVATE_KEY"
    )
    assert preflight._domain(
        "attestation-verifier", "attestation", "development"
    ).endswith("/development")

    parser = preflight._build_parser()
    parser.parse_args(
        [
            "webhook-smoke",
            "--environment",
            "development",
            "--collect-input",
            "/evidence/wave0-development.raw.json",
            "--raw-output",
            "/evidence/webhook-smoke.raw.json",
        ]
    )
    parser.parse_args(
        [
            "redis-load",
            "--environment",
            "development",
            "--raw-output",
            "/evidence/redis-load.raw.json",
        ]
    )
    parser.parse_args(
        [
            "rollback-rehearsal",
            "--environment",
            "development",
            "--raw-output",
            "/evidence/rollback.raw.json",
        ]
    )


def test_fixture_cannot_become_representative_by_flipping_legacy_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    value = json.loads(raw.read_text())
    value["representative"] = True
    value["source_mode"] = "live-read-only"
    write_json(raw, value)
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(tmp_path / "attestation.json"),
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(collected_at="2026-08-06T12:00:00"),
        lambda value: value["window"].update(start="2026-07-07T12:00:01Z"),
        lambda value: value["provenance"].update(environment="production"),
        lambda value: value["aggregates"].update(workers=0),
    ],
)
def test_verify_rejects_invalid_timestamp_window_provenance_or_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    value = write_synthetic_live_evidence(raw)
    mutation(value)
    write_json(raw, value)
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(tmp_path / "invalid.attestation.json"),
            ]
        )
        == 2
    )


def test_optional_approval_is_strict_even_when_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_observations(
        approvals={
            "meta_interruption_acceptance": runtime_text(
                "arbitrary-", "secret", "123456"
            )
        }
    )
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture)
    write_synthetic_live_evidence(raw)
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(tmp_path / "approval.attestation.json"),
            ]
        )
        == 2
    )


def test_scan_tracked_reads_staged_blob_after_worktree_cleanup(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evidence = tmp_path / "config.json"
    evidence.write_text(
        json.dumps({"zapi_token": runtime_text("lower", "case", "12345", "67890")}),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", evidence.name], cwd=tmp_path, check=True)
    evidence.write_text("{}", encoding="utf-8")
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1


def test_scan_tracked_handles_newline_filename_from_index(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evidence = tmp_path / "line\nbreak.txt"
    evidence.write_text(
        runtime_text('credential="', "Opaque", "Value", '9876543210"'),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "--", evidence.name], cwd=tmp_path, check=True)
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1
    error = capsys.readouterr().err
    assert "probable credential value" in error
    assert "raw evidence filename" not in error


def test_scan_tracked_rejects_divergent_literal_backslash_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evidence = tmp_path / "literal\\name.txt"
    evidence.write_text("safe", encoding="utf-8")
    subprocess.run(["git", "add", "--", evidence.name], cwd=tmp_path, check=True)
    evidence.write_text(
        runtime_text("credential=", "Opaque", "Value", "9876543210"),
        encoding="utf-8",
    )
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1
    assert "literal backslash" in capsys.readouterr().err


def test_scan_tracked_fails_closed_on_divergent_symlink(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evidence = tmp_path / "tracked.txt"
    evidence.write_text("safe", encoding="utf-8")
    subprocess.run(["git", "add", "--", evidence.name], cwd=tmp_path, check=True)
    evidence.unlink()
    evidence.symlink_to(tmp_path / "outside.txt")
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1


def test_scan_tracked_fails_closed_on_divergent_oversize_file(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evidence = tmp_path / "tracked.txt"
    evidence.write_text("safe", encoding="utf-8")
    subprocess.run(["git", "add", "--", evidence.name], cwd=tmp_path, check=True)
    evidence.write_bytes(b"x" * (preflight.MAX_TRACKED_FILE_BYTES + 1))
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1


def test_scan_tracked_fails_closed_on_unreadable_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evidence = tmp_path / "tracked.txt"
    evidence.write_text("safe", encoding="utf-8")
    subprocess.run(["git", "add", "--", evidence.name], cwd=tmp_path, check=True)
    evidence.write_text("divergent", encoding="utf-8")

    def unreadable(_root: Path, _relative: str) -> bytes | None:
        raise preflight.PreflightError("mocked permission denial")

    monkeypatch.setattr(preflight, "_safe_worktree_bytes", unreadable)
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1
    assert "mocked permission denial" in capsys.readouterr().err


def test_scan_content_detects_bounded_unquoted_secret_assignment() -> None:
    content = runtime_text("credential=", "AbCdEf", "9876543210")
    assert "probable credential value" in preflight._scan_content(
        Path("innocent.txt"), content
    )


@pytest.mark.parametrize(
    "content",
    [
        "credential=short7",
        "credential=YOUR_CREDENTIAL_HERE",
        "credential=development",
        runtime_text("LLM_API_", "KEY=", "\n", "LLM_MODEL=gpt-4o-mini"),
    ],
)
def test_scan_content_ignores_nonsecret_unquoted_assignments(content: str) -> None:
    assert "probable credential value" not in preflight._scan_content(
        Path("innocent.txt"), content
    )


def test_scan_tracked_enumerates_the_index_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("safe", encoding="utf-8")
    subprocess.run(["git", "add", "--", tracked.name], cwd=tmp_path, check=True)
    original_run = preflight.subprocess.run
    enumerations: list[tuple[str, ...]] = []

    def counting_run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[:2] == ["git", "ls-files"]:
            enumerations.append(tuple(command))
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(preflight.subprocess, "run", counting_run)
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 0
    assert enumerations == [("git", "ls-files", "--cached", "-s", "-z")]


def test_scan_tracked_detects_renamed_raw_evidence_by_schema(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    evidence = tmp_path / "innocent.json"
    write_json(
        evidence,
        {
            "schema_version": "1.0",
            "kind": preflight.RAW_KIND,
            "source_mode": "live-read-only",
        },
    )
    subprocess.run(["git", "add", evidence.name], cwd=tmp_path, check=True)
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1


def test_evidence_directory_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "evidence"
    link.symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(link))
    with pytest.raises(preflight.PreflightError, match="symlink"):
        preflight._evidence_dir()


def test_webhook_target_rejects_private_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_PROVIDER_ZAPI_SMOKE_ALLOWED_HOST", "collector.example")
    monkeypatch.setattr(
        preflight.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(preflight.PreflightError, match="private"):
        preflight._public_https_target("https://collector.example/smoke")


def test_webhook_smoke_zero_active_channel_emits_bound_na_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_observations()
    fixture["postgresql"]["active_zapi_tenants"] = 0
    collect_raw, _ = collect_fixture(tmp_path, monkeypatch, fixture)
    now = preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00")
    write_synthetic_live_evidence(collect_raw, now=now)
    monkeypatch.setattr(preflight, "_utc_now", lambda **kwargs: now)
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "development")
    monkeypatch.setenv("DEPLOYMENT_ID", "deploy-safe-42")

    def unexpected_network(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("zero-active smoke attempted a network call")

    monkeypatch.setattr(preflight, "_http_json", unexpected_network)
    receipt_path = collect_raw.parent / "webhook-smoke.raw.json"
    assert (
        preflight.main(
            [
                "webhook-smoke",
                "--environment",
                "development",
                "--collect-input",
                str(collect_raw),
                "--raw-output",
                str(receipt_path),
            ]
        )
        == 0
    )
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "not_applicable_no_active_channel"
    assert receipt["collect_binding"] == {
        "active_zapi_count": 0,
        "signed_raw_digest": preflight._signed_digest(
            json.loads(collect_raw.read_text())
        ),
    }
    assert receipt["readiness"] == {"pass": False, "reason": "no-active-channel"}
    assert receipt["activation"] == {
        "allowed": False,
        "requires_fresh_smoke_before_activation": True,
    }
    preflight._validate_receipt_shape(receipt, "zero-active receipt")
    preflight._verify_evidence_signature(receipt, "zero-active receipt")


def test_webhook_smoke_active_channel_requires_bound_real_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collect_raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    now = preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00")
    write_synthetic_live_evidence(collect_raw, now=now)
    monkeypatch.setattr(preflight, "_utc_now", lambda **kwargs: now)
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "development")
    monkeypatch.setenv("DEPLOYMENT_ID", "deploy-safe-42")
    monkeypatch.setenv("API_PROVIDER_ZAPI_SMOKE_TRIGGER_URL", "https://collector")
    monkeypatch.setenv("API_PROVIDER_ZAPI_SMOKE_BEARER", "injected")
    monkeypatch.setenv("API_PROVIDER_ZAPI_SENTINEL", "opaque-sentinel-42")
    calls: list[dict[str, Any]] = []

    def callback(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, "kwargs": kwargs})
        return {
            "observed_sentinel": "opaque-sentinel-42",
            "edge_template_only": True,
            "application_leak_categories": ["application-log-raw-request-target"],
            "complete_redaction_pass": False,
            "observed_source_inventory": [
                "controlled-zapi-collector",
                "external-edge-observability",
                "application-observability",
            ],
            "observed_sink_inventory": sorted(preflight.SINK_NAMES),
        }

    monkeypatch.setattr(preflight, "_http_json", callback)
    receipt_path = collect_raw.parent / "active-smoke.raw.json"
    assert (
        preflight.main(
            [
                "webhook-smoke",
                "--environment",
                "development",
                "--collect-input",
                str(collect_raw),
                "--raw-output",
                str(receipt_path),
            ]
        )
        == 0
    )
    assert len(calls) == 1
    receipt = json.loads(receipt_path.read_text())
    assert receipt["status"] == "passed_real_callback"
    assert receipt["collect_binding"]["active_zapi_count"] == 3
    assert receipt["activation"] == {
        "allowed": True,
        "requires_fresh_smoke_before_activation": False,
    }
    with pytest.raises(preflight.PreflightError, match="active count"):
        preflight._webhook_smoke_state(receipt, 0)


def test_webhook_smoke_rejects_runtime_and_collect_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collect_raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    now = preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00")
    write_synthetic_live_evidence(collect_raw, now=now)
    monkeypatch.setattr(preflight, "_utc_now", lambda **kwargs: now)
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "development")
    monkeypatch.setenv("DEPLOYMENT_ID", "other-deployment")
    assert (
        preflight.main(
            [
                "webhook-smoke",
                "--environment",
                "development",
                "--collect-input",
                str(collect_raw),
                "--raw-output",
                str(collect_raw.parent / "mismatch.raw.json"),
            ]
        )
        == 2
    )


def release_attestation(
    environment: str,
    phase: str,
    deployment: str,
    active_zapi_count: int = 3,
) -> dict[str, Any]:
    thresholds = {
        "representative_evidence": {
            "pass": True,
            "reason": "development-or-production-live-evidence-required",
        },
        "fixed_window": {"pass": True, "observed_days": 31, "required_days": 31},
        "zapi_path_roundtrip": {
            "pass": True,
            "status": "passed_real_callback",
            "active_zapi_count": 3,
            "readiness": True,
            "activation": True,
            "requires_fresh_smoke_before_activation": False,
        },
        "external_edge_redaction": {"pass": True},
        "redis_capacity": {
            "pass": True,
            "projected_bytes": 200_000,
            "limit_bytes": 7_000_000,
            "formula_version": preflight.REDIS_CAPACITY_FORMULA,
        },
        "redis_evictions": {"pass": True, "observed": 0, "required": 0},
        "meta_app_secret_readiness": {
            "pass": True,
            "affected_count": 0,
            "reason": "not-required",
        },
        "application_redaction_baseline": {
            "status": "pass",
            "pass": True,
            "recorded": True,
            "leak_categories": [],
        },
    }
    if environment == "production":
        thresholds["zapi_path_roundtrip"] = {
            "pass": False,
            "status": "not-run-pre-cutover",
        }
        thresholds["application_redaction_baseline"] = {
            "status": "not-run-pre-cutover",
            "pass": False,
            "recorded": True,
            "leak_categories": [],
        }
    elif active_zapi_count == 0:
        thresholds["zapi_path_roundtrip"] = {
            "pass": True,
            "status": "not_applicable_no_active_channel",
            "active_zapi_count": 0,
            "readiness": False,
            "activation": False,
            "requires_fresh_smoke_before_activation": True,
        }
        thresholds["application_redaction_baseline"] = {
            "status": "not_applicable_no_active_channel",
            "pass": True,
            "recorded": True,
            "leak_categories": [],
        }
    return {
        "schema_version": "1.0",
        "kind": preflight.ATTESTATION_KIND,
        "phase": phase,
        "environment": environment,
        "deployment_id": deployment,
        "collected_at": "2026-08-06T12:00:00Z",
        "window": {
            "days": 31,
            "start": "2026-07-06T12:00:00Z",
            "end": "2026-08-06T12:00:00Z",
        },
        "source_inventory": sorted(
            preflight._expected_sources(0, phase, active_zapi_count)
        ),
        "sink_inventory": sorted(preflight._expected_sinks(phase, active_zapi_count)),
        "aggregates": {
            "active_meta_count": 2,
            "active_zapi_count": active_zapi_count,
            "meta_missing_app_secret_count": 0,
            "worker_count": 4,
            "p95_unique_messages_day": 31,
            "p95_claim_bytes": 128,
        },
        "threshold_results": thresholds,
        "approvals": {},
        "decision": "go",
        "design_return_criteria": [],
    }


def test_release_decision_requires_bound_closed_development_and_production_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("API_PROVIDER_PREFLIGHT_NOW", raising=False)
    monkeypatch.setattr(
        preflight,
        "_utc_now",
        lambda **kwargs: preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    minimal = tmp_path / "minimal.json"
    write_json(minimal, {"decision": "go"})
    assert (
        preflight.main(
            [
                "release-decision",
                "--development",
                str(minimal),
                "--production",
                str(minimal),
            ]
        )
        == 2
    )
    development = tmp_path / "development.json"
    production = tmp_path / "production.json"
    development_value = release_attestation("development", "release", "development-42")
    production_value = release_attestation("production", "pre-cutover", "production-42")
    preflight._sign_evidence(development_value)
    preflight._sign_evidence(production_value)
    write_json(development, development_value)
    write_json(production, production_value)
    assert (
        preflight.main(
            [
                "release-decision",
                "--development",
                str(development),
                "--production",
                str(production),
            ]
        )
        == 0
    )
    monkeypatch.setenv("DEVELOPMENT_DEPLOYMENT_ID", "other-development")
    assert (
        preflight.main(
            [
                "release-decision",
                "--development",
                str(development),
                "--production",
                str(production),
            ]
        )
        == 2
    )
    monkeypatch.setenv("DEVELOPMENT_DEPLOYMENT_ID", "development-42")
    monkeypatch.setenv("PRODUCTION_DEPLOYMENT_ID", "other-production")
    assert (
        preflight.main(
            [
                "release-decision",
                "--development",
                str(development),
                "--production",
                str(production),
            ]
        )
        == 2
    )


@pytest.mark.parametrize("launder_mode", ["active-receipt", "later-activation"])
def test_release_decision_enforces_zero_active_na_and_fresh_activation_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    launder_mode: str,
) -> None:
    monkeypatch.setattr(
        preflight,
        "_utc_now",
        lambda **kwargs: preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    development = release_attestation(
        "development", "release", "development-zero-42", active_zapi_count=0
    )
    production = release_attestation("production", "pre-cutover", "production-zero-42")
    monkeypatch.setenv("DEVELOPMENT_DEPLOYMENT_ID", "development-zero-42")
    monkeypatch.setenv("PRODUCTION_DEPLOYMENT_ID", "production-zero-42")
    development_path = tmp_path / "development-zero.json"
    production_path = tmp_path / "production-zero.json"
    preflight._sign_evidence(development)
    preflight._sign_evidence(production)
    write_json(development_path, development)
    write_json(production_path, production)
    arguments = [
        "release-decision",
        "--development",
        str(development_path),
        "--production",
        str(production_path),
    ]
    assert preflight.main(arguments) == 0

    if launder_mode == "active-receipt":
        threshold = development["threshold_results"]["zapi_path_roundtrip"]
        threshold.update(
            status="passed_real_callback",
            readiness=True,
            activation=True,
            requires_fresh_smoke_before_activation=False,
        )
    else:
        development["aggregates"]["active_zapi_count"] = 1
    preflight._sign_evidence(development)
    write_json(development_path, development)
    assert preflight.main(arguments) == 2


def test_meta_inventory_query_uses_persisted_provider_value() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    query_section = source[
        source.index("async def _collect_postgres") : source.index(
            "async def _collect_redis"
        )
    ]
    assert "whatsapp_provider' = 'meta_cloud'" in query_section
    assert "whatsapp_provider' = 'meta'" not in query_section


def test_redis_load_production_fails_before_connection_without_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "production")
    monkeypatch.setenv("DEPLOYMENT_ID", "production-42")
    assert (
        preflight.main(
            [
                "redis-load",
                "--environment",
                "production",
                "--raw-output",
                str(evidence / "redis.raw.json"),
            ]
        )
        == 2
    )


def test_redis_load_never_deletes_a_preexisting_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis.asyncio as redis_asyncio

    class FakePipeline:
        def __init__(self, client: Any) -> None:
            self.client = client
            self.operations: list[tuple[str, str, bytes]] = []

        def set(self, key: str, value: bytes, **kwargs: Any) -> None:
            self.operations.append(("set", key, value))

        def eval(self, script: str, key_count: int, key: str, value: bytes) -> None:
            self.operations.append(("eval", key, value))

        def get(self, key: str) -> None:
            self.operations.append(("get", key, b""))

        async def execute(self) -> list[Any]:
            if self.operations[0][0] == "set":
                for index, item in enumerate(self.operations):
                    if index:
                        self.client.values[item[1]] = item[2]
                return [False, True]
            if self.operations[0][0] == "get":
                return [self.client.values.get(item[1]) for item in self.operations]
            for item in self.operations:
                self.client.cleaned.append(item[1])
                self.client.values.pop(item[1], None)
            return [1 for _ in self.operations]

    class FakeRedis:
        def __init__(self) -> None:
            self.cleaned: list[str] = []
            self.values: dict[str, bytes] = {"deploy:run:0": b"preexisting"}

        async def info(self, section: str) -> dict[str, int]:
            return {"evicted_keys": 0, "used_memory": 100}

        def pipeline(self, **kwargs: Any) -> FakePipeline:
            return FakePipeline(self)

        async def scan(self, **kwargs: Any) -> tuple[int, list[str]]:
            return 0, list(self.values)

        async def get(self, key: str) -> bytes | None:
            return self.values.get(key)

        async def aclose(self) -> None:
            return None

    client = FakeRedis()
    monkeypatch.setenv(
        "REDIS_URL",
        runtime_text("redis", "://", "secret-", "manager-", "value"),
    )
    monkeypatch.setattr(redis_asyncio.Redis, "from_url", lambda *args, **kwargs: client)
    with pytest.raises(preflight.PreflightError, match="collided"):
        asyncio.run(preflight._redis_load("deploy:run", 2, 60, 128))
    assert client.cleaned == ["deploy:run:1"]
    assert "deploy:run:0" not in client.cleaned
    assert client.values == {"deploy:run:0": b"preexisting"}


def test_raw_and_attestation_mutation_fail_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    write_synthetic_live_evidence(raw)
    value = json.loads(raw.read_text())
    value["aggregates"]["workers"] = 99
    write_json(raw, value)
    attestation = tmp_path / "mutated.attestation.json"
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(attestation),
            ]
        )
        == 2
    )
    assert not attestation.exists()

    preflight._sign_evidence(value)
    value.pop(preflight.AUTH_FIELD)
    write_json(raw, value)
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(attestation),
            ]
        )
        == 2
    )

    monkeypatch.delenv("API_PROVIDER_PREFLIGHT_NOW", raising=False)
    monkeypatch.setattr(
        preflight,
        "_utc_now",
        lambda **kwargs: preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    development = release_attestation("development", "release", "development-auth")
    production = release_attestation("production", "pre-cutover", "production-auth")
    preflight._sign_evidence(development)
    preflight._sign_evidence(production)
    development["aggregates"]["worker_count"] = 99
    development_path = tmp_path / "development.json"
    production_path = tmp_path / "production.json"
    write_json(development_path, development)
    write_json(production_path, production)
    assert (
        preflight.main(
            [
                "release-decision",
                "--development",
                str(development_path),
                "--production",
                str(production_path),
            ]
        )
        == 2
    )


def test_fixture_is_never_signed_or_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, result = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    assert result == 0
    assert preflight.AUTH_FIELD not in json.loads(raw.read_text())
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(tmp_path / "fixture.attestation.json"),
            ]
        )
        == 2
    )


def test_production_pre_cutover_collect_verify_pipeline_uses_passive_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00")
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)

    def secure_json(name: str, value: dict[str, Any]) -> Path:
        path = evidence / name
        write_json(path, value)
        os.chmod(path, 0o600)
        return path

    message = secure_json(
        "message.json",
        {
            "daily": list(range(1, 32)),
            "p95_claim_bytes": 128,
            "window": {
                "start": "2026-07-06T12:00:00Z",
                "end": "2026-08-06T12:00:00Z",
            },
            "provenance": {
                "source": "message-observability",
                "environment": "production",
                "deployment_id": "production-live-42",
                "observed_at": "2026-08-06T12:00:00Z",
            },
        },
    )
    passive = secure_json(
        "passive.json",
        {
            "provenance": {
                "source": "external-edge-observability",
                "environment": "production",
                "deployment_id": "production-live-42",
                "observed_at": "2026-08-06T12:00:00Z",
            },
            "source_inventory": ["application-observability"],
            "sink_inventory": sorted(preflight.SINK_NAMES),
        },
    )

    async def postgres() -> dict[str, int]:
        return {
            "active_meta_tenants": 2,
            "active_zapi_tenants": 3,
            "meta_missing_app_secret": 0,
        }

    async def redis() -> dict[str, Any]:
        return {
            "used_memory_peak_bytes": 100_000,
            "maxmemory_bytes": 10_000_000,
            "maxmemory_policy": "allkeys-lru",
            "evicted_keys": 0,
        }

    monkeypatch.setattr(preflight, "_collect_postgres", postgres)
    monkeypatch.setattr(preflight, "_collect_redis", redis)
    monkeypatch.setattr(preflight, "_utc_now", lambda **kwargs: now)
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "production")
    monkeypatch.setenv("DEPLOYMENT_ID", "production-live-42")
    monkeypatch.setenv("API_PROVIDER_WORKERS", "4")
    monkeypatch.setenv("API_PROVIDER_MESSAGE_COUNTS_FILE", str(message))
    monkeypatch.setenv("API_PROVIDER_PASSIVE_OBSERVABILITY_FILE", str(passive))
    monkeypatch.delenv("API_PROVIDER_ZAPI_SMOKE_TRIGGER_URL", raising=False)
    raw = evidence / "release-production.raw.json"
    assert (
        preflight.main(
            [
                "collect",
                "--phase",
                "pre-cutover",
                "--environment",
                "production",
                "--deployment-id",
                "production-live-42",
                "--window-days",
                "31",
                "--raw-output",
                str(raw),
            ]
        )
        == 0
    )
    attestation = tmp_path / "production.attestation.json"
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "pre-cutover",
                "--input",
                str(raw),
                "--attestation",
                str(attestation),
            ]
        )
        == 0
    )
    thresholds = json.loads(attestation.read_text())["threshold_results"]
    assert thresholds["zapi_path_roundtrip"] == {
        "pass": False,
        "status": "not-run-pre-cutover",
    }
    assert thresholds["application_redaction_baseline"] == {
        "status": "not-run-pre-cutover",
        "pass": False,
        "recorded": True,
        "leak_categories": [],
    }
    assert (
        "go: pre-cutover external preconditions are satisfied"
        in capsys.readouterr().out
    )


def test_scan_tracked_rejects_unknown_attestation_field_and_secret_note(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    value = release_attestation("development", "release", "scan-42")
    secret_note = runtime_text(
        "to", "ken", "=", "Actual", "Opaque", "Token", "987654321"
    )
    value["note"] = secret_note
    preflight._sign_evidence(value)
    path = tmp_path / "release-development.attestation.json"
    write_json(path, value)
    subprocess.run(["git", "add", path.name], cwd=tmp_path, check=True)
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1

    value.pop("note")
    value["threshold_results"]["representative_evidence"]["reason"] = secret_note
    preflight._sign_evidence(value)
    write_json(path, value)
    subprocess.run(["git", "add", path.name], cwd=tmp_path, check=True)
    assert preflight.main(["scan-tracked", "--root", str(tmp_path)]) == 1


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://other.example/smoke", "allowed host"),
        ("https://collector.example:444/smoke", "port 443"),
    ],
)
def test_webhook_destination_binding_rejects_cross_host_and_alternate_port(
    monkeypatch: pytest.MonkeyPatch, url: str, message: str
) -> None:
    monkeypatch.setenv("API_PROVIDER_ZAPI_SMOKE_ALLOWED_HOST", "collector.example")
    with pytest.raises(preflight.PreflightError, match=message):
        preflight._public_https_target(url)


def test_webhook_response_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def read(self, size: int) -> bytes:
            return b"x" * size

    class Connection:
        def __init__(self, *args: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        preflight,
        "_public_https_target",
        lambda url: ("collector.example", 443, "203.0.113.1", "/smoke"),
    )
    monkeypatch.setattr(preflight, "_PinnedHTTPSConnection", Connection)
    with pytest.raises(preflight.PreflightError, match="too large"):
        preflight._http_json("https://collector.example/smoke", {}, {})


def test_verify_requires_owned_mode_0600_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    write_synthetic_live_evidence(raw)
    os.chmod(raw, 0o644)
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(tmp_path / "mode.attestation.json"),
            ]
        )
        == 2
    )


def test_verify_rejects_runtime_deployment_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    write_synthetic_live_evidence(raw)
    monkeypatch.setenv("DEPLOYMENT_ID", "other-deployment")
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(tmp_path / "mismatch.json"),
            ]
        )
        == 2
    )


def test_redis_rehearsal_population_covers_retained_31_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    observed: dict[str, int] = {}

    async def redis_load(
        prefix: str, count: int, ttl_seconds: int, claim_bytes: int
    ) -> dict[str, Any]:
        observed["count"] = count
        return {
            "attempted_claims": count,
            "claims_per_second": 1.0,
            "memory_delta_bytes": 1,
            "evicted_keys_delta": 0,
            "inserted": count,
            "cleaned": count,
            "residual": 0,
        }

    monkeypatch.setattr(preflight, "_redis_load", redis_load)
    monkeypatch.setattr(
        preflight,
        "_message_volume_from_env",
        lambda *args: {"p95_unique_messages_day": 7, "p95_claim_bytes": 128},
    )
    monkeypatch.setattr(
        preflight,
        "_release_collect_binding",
        lambda *args: {"signed_raw_digest": "a" * 64, "active_zapi_count": 3},
    )
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "development")
    monkeypatch.setenv("DEPLOYMENT_ID", "development-42")
    assert (
        preflight.main(
            [
                "redis-load",
                "--environment",
                "development",
                "--raw-output",
                str(evidence / "redis-load.raw.json"),
            ]
        )
        == 0
    )
    assert observed["count"] == 7 * 31


def test_release_rejects_test_clock_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_PROVIDER_PREFLIGHT_NOW", "2026-08-06T12:00:00Z")
    development = tmp_path / "development.json"
    production = tmp_path / "production.json"
    write_json(development, {})
    write_json(production, {})
    assert (
        preflight.main(
            [
                "release-decision",
                "--development",
                str(development),
                "--production",
                str(production),
            ]
        )
        == 2
    )


def signed_smoke_receipt(
    environment: str = "development", deployment: str = "deploy-42"
) -> dict[str, Any]:
    value = {
        "schema_version": preflight.SCHEMA_VERSION,
        "kind": "api-provider-webhook-smoke-raw",
        "collected_at": "2026-08-06T12:00:00Z",
        "provenance": {
            "source": "controlled-zapi-collector",
            "environment": environment,
            "deployment_id": deployment,
            "observed_at": "2026-08-06T12:00:00Z",
        },
        "status": "passed_real_callback",
        "collect_binding": {
            "signed_raw_digest": "5" * 64,
            "active_zapi_count": 3,
        },
        "readiness": {"pass": True, "reason": "real-callback-smoke"},
        "activation": {
            "allowed": True,
            "requires_fresh_smoke_before_activation": False,
        },
        "source_inventory": [
            "controlled-zapi-collector",
            "external-edge-observability",
            "application-observability",
        ],
        "sink_inventory": sorted(preflight.SINK_NAMES),
        "capability": {
            "zapi_path_roundtrip_pass": True,
            "external_edge_redaction_capable": True,
        },
        "application_redaction_baseline": {
            "status": "expected-fail",
            "complete_redaction_pass": False,
            "leaks": ["application-log-raw-request-target"],
        },
    }
    preflight._sign_evidence(value)
    return value


def write_initial_signed_collect(
    raw: Path,
    *,
    phase: str,
    active_zapi_count: int,
) -> dict[str, Any]:
    value = json.loads(raw.read_text())
    value["phase"] = phase
    value["aggregates"]["active_zapi_tenants"] = active_zapi_count
    value.update(
        representative=True,
        source_mode="live-read-only",
        source_inventory=sorted(
            {
                "postgresql",
                "redis",
                "deployment-runtime",
                "message-observability",
            }
        ),
        sink_inventory=[],
        provenance={
            "mode": "live-read-only",
            "environment": "development",
            "deployment_id": "deploy-safe-42",
        },
        capability={
            "zapi_path_roundtrip_pass": False,
            "external_edge_redaction_capable": False,
        },
        application_redaction_baseline={
            "status": "pending_real_smoke",
            "complete_redaction_pass": False,
            "leaks": [],
        },
        webhook_smoke={
            "status": "pending_real_smoke",
            "active_zapi_count": active_zapi_count,
            "readiness": False,
            "activation": False,
            "requires_fresh_smoke_before_activation": True,
            "collect_raw_digest": "0" * 64,
        },
        source_receipts={
            "postgresql": "0" * 64,
            "redis": "1" * 64,
            "message_observability": "2" * 64,
            "observability": "3" * 64,
            "smoke_collect_input": "0" * 64,
        },
    )
    preflight._sign_evidence(value)
    write_json(raw, value)
    os.chmod(raw, 0o600)
    return value


def bound_smoke_receipt(
    collect: dict[str, Any], *, phase: str, active_zapi_count: int
) -> dict[str, Any]:
    receipt = signed_smoke_receipt(deployment="deploy-safe-42")
    receipt["collect_binding"] = {
        "signed_raw_digest": preflight._signed_digest(collect),
        "active_zapi_count": active_zapi_count,
    }
    if active_zapi_count == 0:
        receipt.update(
            status="not_applicable_no_active_channel",
            source_inventory=[],
            sink_inventory=[],
            readiness={"pass": False, "reason": "no-active-channel"},
            activation={
                "allowed": False,
                "requires_fresh_smoke_before_activation": True,
            },
            capability={
                "zapi_path_roundtrip_pass": False,
                "external_edge_redaction_capable": False,
            },
            application_redaction_baseline={
                "status": "not_applicable_no_active_channel",
                "complete_redaction_pass": False,
                "leaks": [],
            },
        )
    elif phase == "release":
        receipt["application_redaction_baseline"] = {
            "status": "pass",
            "complete_redaction_pass": True,
            "leaks": [],
        }
    preflight._sign_evidence(receipt)
    return receipt


def bound_redis_receipt(collect: dict[str, Any]) -> dict[str, Any]:
    claims = collect["aggregates"]["p95_unique_messages_day"] * 31
    value = {
        "schema_version": preflight.SCHEMA_VERSION,
        "kind": "api-provider-redis-load-raw",
        "collected_at": collect["collected_at"],
        "representative": True,
        "environment": "development",
        "deployment_id": "deploy-safe-42",
        "provenance": {
            "source": "redis",
            "environment": "development",
            "deployment_id": "deploy-safe-42",
            "observed_at": collect["collected_at"],
        },
        "input": {
            "claims": claims,
            "claim_bytes": collect["aggregates"]["p95_claim_bytes"],
        },
        "result": {
            "attempted_claims": claims,
            "claims_per_second": 1.0,
            "memory_delta_bytes": 1,
            "evicted_keys_delta": 0,
            "inserted": claims,
            "cleaned": claims,
            "residual": 0,
        },
        "collect_binding": {
            "signed_raw_digest": preflight._signed_digest(collect),
            "active_zapi_count": collect["aggregates"]["active_zapi_tenants"],
        },
    }
    preflight._sign_evidence(value)
    return value


def bound_rollback_receipt(collect: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "config_cache_only_deleted": True,
        "edge_closed": True,
        "forward_fix_selected": True,
        "legacy_webhook_not_reopened": True,
        "legacy_workers_stopped": True,
        "v2_workers_only": True,
    }
    value = {
        "schema_version": preflight.SCHEMA_VERSION,
        "kind": "api-provider-rollback-rehearsal-raw",
        "collected_at": collect["collected_at"],
        "environment": "development",
        "deployment_id": "deploy-safe-42",
        "provenance": {
            "source": "deployment-runtime",
            "environment": "development",
            "deployment_id": "deploy-safe-42",
            "observed_at": collect["collected_at"],
        },
        "checks": checks,
        "pass": True,
        "collect_binding": {
            "signed_raw_digest": preflight._signed_digest(collect),
            "active_zapi_count": collect["aggregates"]["active_zapi_tenants"],
        },
    }
    preflight._sign_evidence(value)
    return value


@pytest.mark.parametrize("active_zapi_count", [0, 3])
def test_literal_wave0_collect_receipt_verify_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_zapi_count: int,
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    canonical = raw.parent / "wave0-development.raw.json"
    raw.rename(canonical)
    collect = write_initial_signed_collect(
        canonical, phase="wave0", active_zapi_count=active_zapi_count
    )
    write_json(
        canonical.parent / "webhook-smoke.raw.json",
        bound_smoke_receipt(
            collect, phase="wave0", active_zapi_count=active_zapi_count
        ),
    )
    os.chmod(canonical.parent / "webhook-smoke.raw.json", 0o600)
    monkeypatch.setattr(
        preflight,
        "_utc_now",
        lambda **kwargs: preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    attestation = tmp_path / f"wave0-{active_zapi_count}.json"
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input-dir",
                str(canonical.parent),
                "--attestation",
                str(attestation),
            ]
        )
        == 0
    )


def test_literal_initial_collect_ignores_legacy_second_collect_receipt_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    now = preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00")

    async def postgres() -> dict[str, int]:
        return {
            "active_meta_tenants": 2,
            "active_zapi_tenants": 0,
            "meta_missing_app_secret": 0,
        }

    async def redis() -> dict[str, Any]:
        return {
            "used_memory_peak_bytes": 100_000,
            "maxmemory_bytes": 10_000_000,
            "maxmemory_policy": "allkeys-lru",
            "evicted_keys": 0,
        }

    monkeypatch.setattr(preflight, "_collect_postgres", postgres)
    monkeypatch.setattr(preflight, "_collect_redis", redis)
    monkeypatch.setattr(preflight, "_utc_now", lambda **kwargs: now)
    monkeypatch.setattr(
        preflight,
        "_message_volume_from_env",
        lambda *args: {
            "p95_unique_messages_day": 31,
            "p95_claim_bytes": 128,
            "source": "message-observability",
        },
    )
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "development")
    monkeypatch.setenv("DEPLOYMENT_ID", "development-live-42")
    monkeypatch.setenv("API_PROVIDER_WORKERS", "4")
    monkeypatch.setenv(
        "API_PROVIDER_WEBHOOK_SMOKE_FILE", str(evidence / "must-not-be-read.json")
    )
    monkeypatch.setenv(
        "API_PROVIDER_REDIS_REHEARSAL_FILE", str(evidence / "must-not-be-read.json")
    )
    raw = evidence / "wave0-development.raw.json"
    assert (
        preflight.main(
            [
                "collect",
                "--phase",
                "wave0",
                "--environment",
                "development",
                "--deployment-id",
                "development-live-42",
                "--window-days",
                "31",
                "--raw-output",
                str(raw),
            ]
        )
        == 0
    )
    assert (
        json.loads(raw.read_text())["webhook_smoke"]["status"] == "pending_real_smoke"
    )


@pytest.mark.parametrize("active_zapi_count", [0, 3])
def test_literal_release_collect_receipts_verify_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_zapi_count: int,
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    canonical = raw.parent / "release-development.raw.json"
    raw.rename(canonical)
    collect = write_initial_signed_collect(
        canonical, phase="release", active_zapi_count=active_zapi_count
    )
    receipts = {
        "webhook-smoke.raw.json": bound_smoke_receipt(
            collect, phase="release", active_zapi_count=active_zapi_count
        ),
        "redis-load.raw.json": bound_redis_receipt(collect),
        "rollback.raw.json": bound_rollback_receipt(collect),
    }
    for name, receipt in receipts.items():
        write_json(canonical.parent / name, receipt)
        os.chmod(canonical.parent / name, 0o600)
    monkeypatch.setattr(
        preflight,
        "_utc_now",
        lambda **kwargs: preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "release",
                "--input-dir",
                str(canonical.parent),
                "--attestation",
                str(tmp_path / f"release-{active_zapi_count}.json"),
            ]
        )
        == 0
    )


def test_actual_literal_release_producer_chain_uses_one_signed_collect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    now = preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00")

    async def postgres() -> dict[str, int]:
        return {
            "active_meta_tenants": 2,
            "active_zapi_tenants": 3,
            "meta_missing_app_secret": 0,
        }

    async def redis_info() -> dict[str, Any]:
        return {
            "used_memory_peak_bytes": 100_000,
            "maxmemory_bytes": 10_000_000,
            "maxmemory_policy": "allkeys-lru",
            "evicted_keys": 0,
        }

    async def redis_load(
        prefix: str, count: int, ttl_seconds: int, claim_bytes: int
    ) -> dict[str, Any]:
        return {
            "attempted_claims": count,
            "claims_per_second": 1.0,
            "memory_delta_bytes": 1,
            "evicted_keys_delta": 0,
            "inserted": count,
            "cleaned": count,
            "residual": 0,
        }

    monkeypatch.setattr(preflight, "_collect_postgres", postgres)
    monkeypatch.setattr(preflight, "_collect_redis", redis_info)
    monkeypatch.setattr(preflight, "_redis_load", redis_load)
    monkeypatch.setattr(preflight, "_utc_now", lambda **kwargs: now)
    monkeypatch.setattr(
        preflight,
        "_message_volume_from_env",
        lambda *args: {
            "p95_unique_messages_day": 7,
            "p95_claim_bytes": 128,
            "source": "message-observability",
        },
    )
    monkeypatch.setattr(
        preflight,
        "_http_json",
        lambda *args, **kwargs: {
            "observed_sentinel": "opaque-sentinel-42",
            "edge_template_only": True,
            "application_leak_categories": [],
            "complete_redaction_pass": True,
            "observed_source_inventory": [
                "controlled-zapi-collector",
                "external-edge-observability",
                "application-observability",
            ],
            "observed_sink_inventory": sorted(preflight.SINK_NAMES),
        },
    )
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv("API_PROVIDER_RUNTIME_ENV", "development")
    monkeypatch.setenv("DEPLOYMENT_ID", "development-chain-42")
    monkeypatch.setenv("API_PROVIDER_WORKERS", "4")
    monkeypatch.setenv("API_PROVIDER_ZAPI_SMOKE_TRIGGER_URL", "https://collector")
    monkeypatch.setenv("API_PROVIDER_ZAPI_SMOKE_BEARER", "injected")
    monkeypatch.setenv("API_PROVIDER_ZAPI_SENTINEL", "opaque-sentinel-42")
    checklist = evidence / "rollback-checklist.json"
    write_json(
        checklist,
        {
            "kind": "api-provider-rollback-observation",
            "environment": "development",
            "deployment_id": "development-chain-42",
            "observed_at": "2026-08-06T12:00:00Z",
            "checks": {name: True for name in preflight.ROLLBACK_CHECKS},
        },
    )
    os.chmod(checklist, 0o600)
    monkeypatch.setenv("API_PROVIDER_ROLLBACK_CHECKLIST_FILE", str(checklist))

    collect_path = evidence / "release-development.raw.json"
    assert (
        preflight.main(
            [
                "collect",
                "--phase",
                "release",
                "--environment",
                "development",
                "--deployment-id",
                "development-chain-42",
                "--window-days",
                "31",
                "--raw-output",
                str(collect_path),
            ]
        )
        == 0
    )
    assert (
        preflight.main(
            [
                "redis-load",
                "--environment",
                "development",
                "--raw-output",
                str(evidence / "redis-load.raw.json"),
            ]
        )
        == 0
    )
    assert (
        preflight.main(
            [
                "webhook-smoke",
                "--environment",
                "development",
                "--collect-input",
                str(collect_path),
                "--raw-output",
                str(evidence / "webhook-smoke.raw.json"),
            ]
        )
        == 0
    )
    assert (
        preflight.main(
            [
                "rollback-rehearsal",
                "--environment",
                "development",
                "--raw-output",
                str(evidence / "rollback.raw.json"),
            ]
        )
        == 0
    )
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "release",
                "--input-dir",
                str(evidence),
                "--attestation",
                str(tmp_path / "release-development.attestation.json"),
            ]
        )
        == 0
    )
    assert "go: release external preconditions are satisfied" in capsys.readouterr().out


def test_verify_rejects_wrong_and_swapped_receipt_kinds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    canonical = raw.parent / "wave0-development.raw.json"
    raw.rename(canonical)
    collect = write_initial_signed_collect(
        canonical, phase="wave0", active_zapi_count=3
    )
    wrong = bound_redis_receipt(collect)
    write_json(canonical.parent / "webhook-smoke.raw.json", wrong)
    os.chmod(canonical.parent / "webhook-smoke.raw.json", 0o600)
    monkeypatch.setattr(
        preflight,
        "_utc_now",
        lambda **kwargs: preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input-dir",
                str(canonical.parent),
                "--attestation",
                str(tmp_path / "wrong.json"),
            ]
        )
        == 2
    )
    release_raw = canonical.parent / "release-development.raw.json"
    canonical.rename(release_raw)
    release_collect = write_initial_signed_collect(
        release_raw, phase="release", active_zapi_count=3
    )
    swapped = {
        "redis-load.raw.json": bound_smoke_receipt(
            release_collect, phase="release", active_zapi_count=3
        ),
        "webhook-smoke.raw.json": bound_redis_receipt(release_collect),
        "rollback.raw.json": bound_rollback_receipt(release_collect),
    }
    for name, receipt in swapped.items():
        write_json(release_raw.parent / name, receipt)
        os.chmod(release_raw.parent / name, 0o600)
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "release",
                "--input-dir",
                str(release_raw.parent),
                "--attestation",
                str(tmp_path / "swapped.json"),
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda receipt: receipt.update(status="passed_real_callback"),
        lambda receipt: receipt["readiness"].update(pass_=True),
        lambda receipt: receipt["activation"].update(allowed=True),
        lambda receipt: receipt["activation"].update(
            requires_fresh_smoke_before_activation=False
        ),
    ],
)
def test_verify_rejects_every_zero_active_cross_matrix_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Any,
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    canonical = raw.parent / "wave0-development.raw.json"
    raw.rename(canonical)
    collect = write_initial_signed_collect(
        canonical, phase="wave0", active_zapi_count=0
    )
    receipt = bound_smoke_receipt(collect, phase="wave0", active_zapi_count=0)
    mutation(receipt)
    if "pass_" in receipt["readiness"]:
        receipt["readiness"]["pass"] = receipt["readiness"].pop("pass_")
    preflight._sign_evidence(receipt)
    receipt_path = canonical.parent / "webhook-smoke.raw.json"
    write_json(receipt_path, receipt)
    os.chmod(receipt_path, 0o600)
    monkeypatch.setattr(
        preflight,
        "_utc_now",
        lambda **kwargs: preflight.datetime.fromisoformat("2026-08-06T12:00:00+00:00"),
    )
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input-dir",
                str(canonical.parent),
                "--attestation",
                str(tmp_path / "invalid-zero.json"),
            ]
        )
        == 2
    )


def test_receipts_require_closed_schema_signature_and_role_environment_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = signed_smoke_receipt()
    preflight._validate_receipt_shape(receipt, "smoke")
    preflight._verify_evidence_signature(receipt, "smoke")
    private_name = "API_PROVIDER_RECEIPT_WEBHOOK_SMOKE_DEVELOPMENT_PRIVATE_KEY"
    monkeypatch.delenv(private_name)
    preflight._verify_evidence_signature(receipt, "smoke")

    public_name = "API_PROVIDER_RECEIPT_WEBHOOK_SMOKE_DEVELOPMENT_PUBLIC_KEY"
    original_public = os.environ[public_name]
    _, wrong_public = _key_pair()
    monkeypatch.setenv(public_name, wrong_public)
    with pytest.raises(preflight.PreflightError, match="key id"):
        preflight._verify_evidence_signature(receipt, "wrong-key")
    monkeypatch.setenv(public_name, original_public)

    unsigned = dict(receipt)
    unsigned.pop(preflight.AUTH_FIELD)
    with pytest.raises(preflight.PreflightError):
        preflight._validate_receipt_shape(unsigned, "smoke")
    altered = json.loads(json.dumps(receipt))
    altered["capability"]["zapi_path_roundtrip_pass"] = False
    with pytest.raises(preflight.PreflightError, match="authenticate"):
        preflight._verify_evidence_signature(altered, "smoke")
    domain_mismatch = json.loads(json.dumps(receipt))
    domain_mismatch[preflight.AUTH_FIELD]["domain"] += "/other"
    with pytest.raises(preflight.PreflightError, match="domain"):
        preflight._verify_evidence_signature(domain_mismatch, "domain")
    swapped = json.loads(json.dumps(receipt))
    swapped["kind"] = "api-provider-redis-load-raw"
    with pytest.raises(preflight.PreflightError):
        preflight._verify_evidence_signature(swapped, "swapped")
    wrong_environment = json.loads(json.dumps(receipt))
    wrong_environment["provenance"]["environment"] = "production"
    with pytest.raises(preflight.PreflightError):
        preflight._verify_evidence_signature(wrong_environment, "cross-environment")

    private, public = _key_pair()
    monkeypatch.setenv(private_name, private)
    monkeypatch.setenv(public_name, public)
    swapped_same_deployment = signed_smoke_receipt(deployment="deploy-42")
    swapped_same_deployment["capability"]["zapi_path_roundtrip_pass"] = False
    preflight._sign_evidence(swapped_same_deployment)
    with pytest.raises(preflight.PreflightError, match="signed raw digest"):
        preflight._verify_receipt_digest(
            {"observability": preflight._signed_digest(receipt)},
            "observability",
            swapped_same_deployment,
            "webhook-smoke.raw.json",
        )


def test_signed_stale_raw_cannot_be_revived_by_fixture_clock_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, _ = collect_fixture(tmp_path, monkeypatch, fixture_observations())
    value = write_synthetic_live_evidence(raw)
    value["collected_at"] = "2020-01-31T00:00:00Z"
    value["window"] = {
        "days": 31,
        "start": "2019-12-31T00:00:00Z",
        "end": "2020-01-31T00:00:00Z",
    }
    preflight._sign_evidence(value)
    write_json(raw, value)
    monkeypatch.setenv("API_PROVIDER_PREFLIGHT_NOW", "2020-01-31T00:00:00Z")
    assert (
        preflight.main(
            [
                "verify",
                "--phase",
                "wave0",
                "--input",
                str(raw),
                "--attestation",
                str(tmp_path / "replay.json"),
            ]
        )
        == 2
    )


@pytest.mark.parametrize("field", ["reason", "formula_version", "status"])
def test_scanner_rejects_bare_secrets_in_free_form_attestation_leaves(
    field: str,
) -> None:
    value = release_attestation("development", "release", "development-safe-42")
    if field == "reason":
        value["threshold_results"]["representative_evidence"][field] = runtime_text(
            "s", "k-", "12345678", "90abcdef"
        )
    elif field == "formula_version":
        value["threshold_results"]["redis_capacity"][field] = runtime_text(
            "A8s9D7f6", "G5h4J3k2", "L1m0N9b8", "V7c6X5z4", "Q3w2E1r0"
        )
    else:
        value["threshold_results"]["application_redaction_baseline"][field] = (
            runtime_text(
                "Bear", "er ", "eyJhbGciOiJIUzI1NiJ9", ".pay", "load.sign", "ature"
            )
        )
    preflight._sign_evidence(value)
    preflight._validate_attestation_shape(value)
    with pytest.raises(preflight.PreflightError, match="secret"):
        preflight._assert_attestation_safe(value)


def test_redis_cleanup_discovers_write_after_lost_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis.asyncio as redis_asyncio

    class Pipeline:
        def __init__(self, client: Any) -> None:
            self.client = client
            self.operations: list[tuple[str, str, bytes]] = []

        def set(self, key: str, value: bytes, **kwargs: Any) -> None:
            self.operations.append(("set", key, value))

        def eval(self, script: str, count: int, key: str, value: bytes) -> None:
            self.operations.append(("eval", key, value))

        def get(self, key: str) -> None:
            self.operations.append(("get", key, b""))

        async def execute(self) -> list[Any]:
            if self.operations[0][0] == "set":
                for _, key, value in self.operations:
                    self.client.values[key] = value
                raise TimeoutError("reply lost after write")
            if self.operations[0][0] == "get":
                return [self.client.values.get(item[1]) for item in self.operations]
            for _, key, value in self.operations:
                if self.client.values.get(key) == value:
                    del self.client.values[key]
            return [1] * len(self.operations)

    class Redis:
        def __init__(self) -> None:
            self.values = {"run:preexisting": b"not-owned"}

        async def info(self, section: str) -> dict[str, int]:
            return {"evicted_keys": 0, "used_memory": 100}

        def pipeline(self, **kwargs: Any) -> Pipeline:
            return Pipeline(self)

        async def scan(self, **kwargs: Any) -> tuple[int, list[str]]:
            return 0, list(self.values)

        async def get(self, key: str) -> bytes | None:
            return self.values.get(key)

        async def aclose(self) -> None:
            return None

    client = Redis()
    monkeypatch.setenv("REDIS_URL", runtime_text("redis", "://", "injected"))
    monkeypatch.setattr(redis_asyncio.Redis, "from_url", lambda *args, **kwargs: client)
    with pytest.raises(
        preflight.PreflightError,
        match="inserted=2 cleaned=2 residual=0",
    ):
        asyncio.run(preflight._redis_load("run", 2, 60, 128))
    assert client.values == {"run:preexisting": b"not-owned"}


def test_redis_ownership_discovery_reads_in_bounded_pipeline_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis.asyncio as redis_asyncio

    candidate_count = 2_500

    class Pipeline:
        def __init__(self, client: Any) -> None:
            self.client = client
            self.operation = ""
            self.items: list[tuple[str, bytes]] = []

        def set(self, key: str, value: bytes, **kwargs: Any) -> None:
            self.operation = "set"
            self.items.append((key, value))

        def get(self, key: str) -> None:
            self.operation = "get"
            self.items.append((key, b""))

        def eval(self, script: str, count: int, key: str, value: bytes) -> None:
            self.operation = "eval"
            self.items.append((key, value))

        async def execute(self) -> list[Any]:
            self.client.executions[self.operation] += 1
            if self.operation == "set":
                for key, value in self.items:
                    self.client.values[key] = value
                return [True] * len(self.items)
            if self.operation == "get":
                return [self.client.values.get(key) for key, _ in self.items]
            results = []
            for key, value in self.items:
                owned = self.client.values.get(key) == value
                if owned:
                    del self.client.values[key]
                results.append(1 if owned else 0)
            return results

    class Redis:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {"run:foreign": b"foreign"}
            self.executions = {"set": 0, "get": 0, "eval": 0}

        async def info(self, section: str) -> dict[str, int]:
            return {"evicted_keys": 0, "used_memory": 100}

        def pipeline(self, **kwargs: Any) -> Pipeline:
            return Pipeline(self)

        async def scan(self, **kwargs: Any) -> tuple[int, list[str]]:
            return 0, list(self.values)

        async def get(self, key: str) -> bytes | None:
            raise AssertionError("ownership discovery performed a sequential GET")

        async def aclose(self) -> None:
            return None

    client = Redis()
    monkeypatch.setenv("REDIS_URL", runtime_text("redis", "://", "injected"))
    monkeypatch.setattr(redis_asyncio.Redis, "from_url", lambda *args, **kwargs: client)
    result = asyncio.run(preflight._redis_load("run", candidate_count, 60, 128))
    batches = math.ceil((candidate_count + 1) / preflight.REDIS_PIPELINE_SIZE)
    assert client.executions["get"] <= batches * 2
    assert result["inserted"] == candidate_count
    assert client.values == {"run:foreign": b"foreign"}


def test_cli_paths_reject_parent_segments_and_symlink_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(preflight.PreflightError, match="lexical"):
        preflight._raw_output(str(tmp_path / "evidence" / ".." / "outside.json"))
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    target = tmp_path / "outside.json"
    write_json(target, {})
    os.chmod(target, 0o600)
    link = evidence / "raw.json"
    link.symlink_to(target)
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    with pytest.raises(preflight.PreflightError, match="securely open|symlink"):
        preflight._secure_json_under(evidence, Path("raw.json"))


def test_secure_json_under_rejects_lexical_escape_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    write_json(outside, {})
    os.chmod(outside, 0o600)

    def unexpected_open(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("unsafe path reached os.open")

    monkeypatch.setattr(os, "open", unexpected_open)
    for unsafe in (
        "../outside.json",
        "/proc/../etc/passwd",
        "nested/./evidence.json",
        "nested//evidence.json",
    ):
        with pytest.raises(preflight.PreflightError, match="safe relative path"):
            preflight._secure_json_under(evidence, unsafe)


def test_external_evidence_rejects_parent_segments_before_secure_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    write_json(outside, {})
    os.chmod(outside, 0o600)
    monkeypatch.setenv("API_PROVIDER_EVIDENCE_DIR", str(evidence))
    monkeypatch.setenv(
        "API_PROVIDER_MESSAGE_COUNTS_FILE", str(evidence / ".." / "outside.json")
    )

    def unexpected_secure_open(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("unsafe environment path reached secure open")

    monkeypatch.setattr(preflight, "_secure_json_under", unexpected_secure_open)
    with pytest.raises(preflight.PreflightError, match="unsafe lexical"):
        preflight._external_evidence_json(
            "API_PROVIDER_MESSAGE_COUNTS_FILE", "message-count evidence"
        )
