"""Tests for the fail-closed media release gate.

The gate must reject every category of failure its policy forbids:
missing tools, missing environment, missing evidence, failing
dependency check, failing or non-JSON pip-audit, non-zero or
skipped pytest, malformed JUnit, sensitive patterns in the tracked
tree, tracked release artifacts, etc. Every category below plants a
single failure and proves the gate refuses to clear the release.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_media_release.py"
SPEC = importlib.util.spec_from_file_location("check_media_release", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
# Register the module in sys.modules so @dataclass can resolve the owning
# module when it introspects the class via sys.modules.get(cls.__module__).
sys.modules["check_media_release"] = release
SPEC.loader.exec_module(release)


# ---------------------------------------------------------------------------
# Helpers for deterministic subprocess simulation
# ---------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class ScriptedRunner:
    """Callable that returns pre-canned subprocess results per command key."""

    def __init__(
        self, mapping: Mapping[tuple[str, ...], subprocess.CompletedProcess[str]]
    ) -> None:
        self._mapping = mapping
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((tuple(cmd), cwd))
        key = _key_for(cmd)
        if key not in self._mapping:
            raise AssertionError(f"unexpected subprocess call: {cmd}")
        return self._mapping[key]


def _key_for(cmd: Sequence[str]) -> tuple[str, ...]:
    cmd = tuple(cmd)
    if cmd[:2] == ("git", "ls-files"):
        return cmd  # includes the subpath (empty or '.artifacts')
    if cmd[:4] == ("poetry", "run", "python", "scripts/check_media_dependencies.py"):
        return ("dependencies",)
    if cmd[:4] == ("poetry", "run", "pip-audit", "--local"):
        return ("pip-audit",)
    if cmd[:3] == ("poetry", "run", "pytest"):
        for token in cmd:
            if token.startswith("--junitxml="):
                return ("pytest", token)
        return ("pytest",)
    return cmd


def _tree_scripted(
    *,
    deps_rc: int = 0,
    audit_rc: int = 0,
    audit_stdout: str = '{"dependencies": []}',
    pytest_rc: int = 0,
    tracked_files: str = "",
    artifacts_tracked: str = "",
) -> ScriptedRunner:
    scripted: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        ("dependencies",): _completed(deps_rc),
        ("pip-audit",): _completed(audit_rc, stdout=audit_stdout),
        ("git", "ls-files"): _completed(0, stdout=tracked_files),
        ("git", "ls-files", ".artifacts"): _completed(0, stdout=artifacts_tracked),
    }
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = (
            f"--junitxml={release.REPOSITORY_ROOT}/.artifacts/media-release/{name}.xml"
        )
        scripted[("pytest", junit_arg)] = _completed(pytest_rc)
    return ScriptedRunner(scripted)


def _resolver(available: Sequence[str]) -> object:
    def resolver(tool: str) -> str | None:
        return f"/usr/bin/{tool}" if tool in available else None

    return resolver


def _junit_parser(
    *,
    tests: int = 1,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
    xfailed: int = 0,
) -> object:
    def parser(path: Path) -> dict[str, int]:  # noqa: ARG001
        return {
            "tests": tests,
            "failures": failures,
            "errors": errors,
            "skipped": skipped,
            "xfailed": xfailed,
        }

    return parser


def _write_valid_evidence(path: Path) -> None:
    sections = "\n\n".join(
        f"## {heading}\n\nreal content for {heading}."
        for heading in release.REQUIRED_EVIDENCE_SECTIONS
    )
    body = (
        "# Media release evidence\n\n"
        f"{sections}\n\n"
        "fixture backup restore DB WAL measurements captured 2026-10-08.\n"
    )
    path.write_text(body, encoding="utf-8")


def _default_env() -> dict[str, str]:
    return {name: "value" for name in release.REQUIRED_ENV}


# ---------------------------------------------------------------------------
# Stage: verify_tools
# ---------------------------------------------------------------------------


def test_verify_tools_blocks_when_tool_missing() -> None:
    result = release.verify_tools(which=_resolver(["poetry"]))

    assert result.blocked is True
    assert any(
        "required tool is missing from PATH" in finding.detail
        for finding in result.findings
    )


def test_verify_tools_passes_when_all_resolved() -> None:
    result = release.verify_tools(required=("poetry",), which=_resolver(["poetry"]))

    assert result.blocked is False
    assert result.failed is False


# ---------------------------------------------------------------------------
# Stage: verify_environment
# ---------------------------------------------------------------------------


def test_verify_environment_blocks_when_env_missing() -> None:
    result = release.verify_environment(env={})

    assert result.blocked is True
    assert any(
        "POSTGRES_SMOKE_DATABASE_URL" in finding.detail for finding in result.findings
    )


def test_verify_environment_passes_when_env_set() -> None:
    result = release.verify_environment(env={"POSTGRES_SMOKE_DATABASE_URL": "x"})

    assert result.blocked is False
    assert result.failed is False


# ---------------------------------------------------------------------------
# Stage: verify_evidence
# ---------------------------------------------------------------------------


def test_verify_evidence_blocks_when_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope.md"

    result = release.verify_evidence(missing)

    assert result.blocked is True
    assert result.failed is False


def test_verify_evidence_fails_when_empty(tmp_path: Path) -> None:
    empty = tmp_path / "evidence.md"
    empty.write_text("", encoding="utf-8")

    result = release.verify_evidence(empty)

    assert result.failed is True


def test_verify_evidence_fails_without_required_sections(tmp_path: Path) -> None:
    partial = tmp_path / "evidence.md"
    partial.write_text("# just a title\n\n## Commands\n\nok.\n", encoding="utf-8")

    result = release.verify_evidence(partial)

    assert result.failed is True
    missing_sections = {
        heading
        for heading in release.REQUIRED_EVIDENCE_SECTIONS
        if heading != "Commands"
    }
    for heading in missing_sections:
        assert any(
            f"missing section: {heading}" in finding.detail
            for finding in result.findings
        )


def test_verify_evidence_fails_without_measurement_phrases(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "evidence.md"
    sections = "\n\n".join(
        f"## {heading}\n\nreal content."
        for heading in release.REQUIRED_EVIDENCE_SECTIONS
    )
    doc.write_text(f"# title\n\n{sections}\n", encoding="utf-8")

    result = release.verify_evidence(doc)

    assert result.failed is True
    assert any("missing anchor phrase" in finding.detail for finding in result.findings)


def test_verify_evidence_blocks_on_placeholder_markers(tmp_path: Path) -> None:
    """Placeholders indicate no external measurement was collected yet.

    Per wish policy, absence of external measurement is BLOCKED (exit 2),
    not a hard failure. The operator receives a specific placeholder
    label so they know which marker to replace with real data.
    """

    doc = tmp_path / "evidence.md"
    _write_valid_evidence(doc)
    polluted = doc.read_text(encoding="utf-8") + "\nTBD section fill with data.\n"
    doc.write_text(polluted, encoding="utf-8")

    result = release.verify_evidence(doc)

    assert result.failed is False
    assert result.blocked is True
    assert any("placeholder" in finding.detail for finding in result.findings)


def test_verify_evidence_accepts_well_formed_document(tmp_path: Path) -> None:
    doc = tmp_path / "evidence.md"
    _write_valid_evidence(doc)

    result = release.verify_evidence(doc)

    assert result.failed is False
    assert result.blocked is False


# ---------------------------------------------------------------------------
# Stage: run_dependency_check
# ---------------------------------------------------------------------------


def test_run_dependency_check_passes_on_zero_exit() -> None:
    runner = ScriptedRunner({("dependencies",): _completed(0, stdout="ok")})

    result = release.run_dependency_check(runner=runner)

    assert result.failed is False


def test_run_dependency_check_fails_on_non_zero() -> None:
    runner = ScriptedRunner(
        {("dependencies",): _completed(1, stderr="pyproject out of range")}
    )

    result = release.run_dependency_check(runner=runner)

    assert result.failed is True
    assert any(
        "check_media_dependencies.py exited 1" in finding.detail
        for finding in result.findings
    )


# ---------------------------------------------------------------------------
# Stage: run_pip_audit
# ---------------------------------------------------------------------------


def test_run_pip_audit_fails_when_subprocess_exits_non_zero() -> None:
    runner = ScriptedRunner(
        {
            ("pip-audit",): _completed(
                1,
                stdout='{"dependencies": [{"name": "foo", "vulns": [{"id": "X"}]}]}',
                stderr="advisory found",
            )
        }
    )

    result = release.run_pip_audit(runner=runner)

    assert result.failed is True
    # Both the non-zero exit and the advisory count must be reported.
    details = " ".join(finding.detail for finding in result.findings)
    assert "pip-audit --local reported failure" in details
    assert "1 advisory" in details


def test_run_pip_audit_fails_when_advisories_present_even_on_zero_exit() -> None:
    # Simulate a buggy stub that returns 0 but reports advisories in JSON.
    runner = ScriptedRunner(
        {
            ("pip-audit",): _completed(
                0,
                stdout=(
                    '{"dependencies": '
                    '[{"name": "a", "vulns": [{"id": "X"}, {"id": "Y"}]}]}'
                ),
            )
        }
    )

    result = release.run_pip_audit(runner=runner)

    assert result.failed is True
    assert any("2 advisor" in finding.detail for finding in result.findings)


def test_run_pip_audit_fails_when_output_not_json() -> None:
    runner = ScriptedRunner({("pip-audit",): _completed(0, stdout="this is not json")})

    result = release.run_pip_audit(runner=runner)

    assert result.failed is True
    assert any(
        "could not be parsed as JSON" in finding.detail for finding in result.findings
    )


def test_run_pip_audit_passes_on_empty_dependencies_json() -> None:
    runner = ScriptedRunner(
        {("pip-audit",): _completed(0, stdout='{"dependencies": []}')}
    )

    result = release.run_pip_audit(runner=runner)

    assert result.failed is False


def test_run_pip_audit_passes_on_local_only_noise() -> None:
    """Local project missing from PyPI must not be treated as advisory."""

    noise = (
        "ERROR:pip_audit._cli:neurostack-api: Dependency not found on "
        "PyPI and could not be audited: neurostack-api (0.1.0)"
    )
    runner = ScriptedRunner(
        {("pip-audit",): _completed(1, stdout='{"dependencies": []}', stderr=noise)}
    )

    result = release.run_pip_audit(runner=runner)

    assert result.failed is False
    assert any("ignored local-only" in f.detail for f in result.findings)


def test_run_pip_audit_fails_on_other_stderr_even_without_json_advisory() -> None:
    """Non-zero exit from an unrelated error still fails the gate."""

    runner = ScriptedRunner(
        {
            ("pip-audit",): _completed(
                1,
                stdout='{"dependencies": []}',
                stderr="ERROR: unexpected failure contacting advisory source",
            )
        }
    )

    result = release.run_pip_audit(runner=runner)

    assert result.failed is True
    assert any("reported failure" in f.detail for f in result.findings)


# ---------------------------------------------------------------------------
# Stage: _parse_junit
# ---------------------------------------------------------------------------


def test_parse_junit_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.xml"

    with pytest.raises(release.ReleaseError):
        release._parse_junit(missing)


def test_parse_junit_rejects_malformed_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.xml"
    bad.write_text("<not-xml", encoding="utf-8")

    with pytest.raises(release.ReleaseError):
        release._parse_junit(bad)


def test_parse_junit_reads_counts_and_xfail(tmp_path: Path) -> None:
    doc = tmp_path / "ok.xml"
    doc.write_text(
        """<testsuites>
          <testsuite tests="3" failures="0" errors="0" skipped="1">
            <testcase name="passes"/>
            <testcase name="xfails">
              <skipped type="pytest.xfail" message="expected failure"/>
            </testcase>
            <testcase name="normal"/>
          </testsuite>
        </testsuites>""",
        encoding="utf-8",
    )

    counts = release._parse_junit(doc)

    assert counts["tests"] == 3
    assert counts["skipped"] == 1
    assert counts["xfailed"] == 1


# ---------------------------------------------------------------------------
# Stage: run_focused_tests
# ---------------------------------------------------------------------------


def test_run_focused_tests_fails_when_pytest_returns_non_zero(
    tmp_path: Path,
) -> None:
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={tmp_path}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(1, stderr="suite failed")
    runner = ScriptedRunner(runner_mapping)

    result = release.run_focused_tests(
        junit_dir=tmp_path,
        runner=runner,
        junit_parser=_junit_parser(tests=1, failures=1),
    )

    assert result.failed is True
    assert any("pytest exited 1" in f.detail for f in result.findings)
    assert any("failures=1" in f.detail for f in result.findings)
    # Cleanup must still erase the junit dir.
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


def test_run_focused_tests_rejects_skipped_scenario(tmp_path: Path) -> None:
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={tmp_path}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    result = release.run_focused_tests(
        junit_dir=tmp_path,
        runner=runner,
        junit_parser=_junit_parser(tests=5, skipped=1),
    )

    assert result.failed is True
    assert any("skip" in f.detail for f in result.findings)


def test_run_focused_tests_rejects_xfailed_scenario(tmp_path: Path) -> None:
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={tmp_path}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    result = release.run_focused_tests(
        junit_dir=tmp_path,
        runner=runner,
        junit_parser=_junit_parser(tests=5, xfailed=1),
    )

    assert result.failed is True
    assert any("xfail" in f.detail for f in result.findings)


def test_run_focused_tests_rejects_missing_junit(tmp_path: Path) -> None:
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={tmp_path}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    def _raise(path: Path) -> dict[str, int]:  # noqa: ARG001
        raise release.ReleaseError("JUnit file is missing")

    result = release.run_focused_tests(
        junit_dir=tmp_path, runner=runner, junit_parser=_raise
    )

    assert result.failed is True
    assert any("JUnit invalid" in f.detail for f in result.findings)


def test_run_focused_tests_cleans_up_junit_dir_on_success(tmp_path: Path) -> None:
    base = tmp_path / "junit-release"
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={base}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    result = release.run_focused_tests(
        junit_dir=base, runner=runner, junit_parser=_junit_parser(tests=4)
    )

    assert result.failed is False
    assert not base.exists()


def test_run_focused_tests_cleans_up_junit_dir_on_failure(
    tmp_path: Path,
) -> None:
    base = tmp_path / "junit-fail"
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {}
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={base}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(1)
    runner = ScriptedRunner(runner_mapping)

    result = release.run_focused_tests(
        junit_dir=base, runner=runner, junit_parser=_junit_parser(tests=1, failures=1)
    )

    assert result.failed is True
    assert not base.exists()


# ---------------------------------------------------------------------------
# Stage: scan_tracked_tree
# ---------------------------------------------------------------------------


def test_scan_tracked_tree_detects_private_key(tmp_path: Path) -> None:
    repo = tmp_path
    leak = repo / "secret.txt"
    leak.write_text(
        "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n",
        encoding="utf-8",
    )
    runner = ScriptedRunner({("git", "ls-files"): _completed(0, stdout="secret.txt\n")})

    result = release.scan_tracked_tree(repository_root=repo, runner=runner)

    assert result.failed is True
    assert any("age private identity" in f.detail for f in result.findings)


def test_scan_tracked_tree_skips_binary_suffixes(tmp_path: Path) -> None:
    repo = tmp_path
    binary = repo / "image.png"
    binary.write_bytes(b"not a real png but has AGE-SECRET-KEY-1xxxxxxxxxxxxxxxxxxxxx")
    runner = ScriptedRunner({("git", "ls-files"): _completed(0, stdout="image.png\n")})

    result = release.scan_tracked_tree(repository_root=repo, runner=runner)

    assert result.failed is False


def test_scan_tracked_tree_fails_when_git_errors(tmp_path: Path) -> None:
    runner = ScriptedRunner({("git", "ls-files"): _completed(128, stderr="not a repo")})

    result = release.scan_tracked_tree(repository_root=tmp_path, runner=runner)

    assert result.failed is True
    assert any("git ls-files failed" in f.detail for f in result.findings)


def test_scan_tracked_tree_scans_extra_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "evidence.md"
    external.write_text(
        "fake JWT eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMiLCJuYW1lIjoiRm9vIn0.whatever_signature_blob\n",
        encoding="utf-8",
    )
    runner = ScriptedRunner({("git", "ls-files"): _completed(0, stdout="")})

    result = release.scan_tracked_tree(
        repository_root=repo, extra_paths=(external,), runner=runner
    )

    assert result.failed is True
    assert any("jwt token" in f.detail for f in result.findings)


# ---------------------------------------------------------------------------
# Stage: ensure_release_artifacts_ignored
# ---------------------------------------------------------------------------


def test_ensure_release_artifacts_ignored_fails_when_tracked() -> None:
    runner = ScriptedRunner(
        {
            ("git", "ls-files", ".artifacts"): _completed(
                0, stdout=".artifacts/media-release/foo.xml\n"
            )
        }
    )

    result = release.ensure_release_artifacts_ignored(runner=runner)

    assert result.failed is True
    assert any("release artifacts are tracked" in f.detail for f in result.findings)


def test_ensure_release_artifacts_ignored_passes_when_empty() -> None:
    runner = ScriptedRunner(
        {("git", "ls-files", ".artifacts"): _completed(0, stdout="")}
    )

    result = release.ensure_release_artifacts_ignored(runner=runner)

    assert result.failed is False


# ---------------------------------------------------------------------------
# execute_gate — happy path and BLOCKED path
# ---------------------------------------------------------------------------


def test_execute_gate_passes_end_to_end(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.md"
    _write_valid_evidence(evidence)
    junit_dir = tmp_path / "junit"
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        ("dependencies",): _completed(0),
        ("pip-audit",): _completed(0, stdout='{"dependencies": []}'),
        ("git", "ls-files"): _completed(0, stdout=""),
        ("git", "ls-files", ".artifacts"): _completed(0, stdout=""),
    }
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={junit_dir}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    exit_code, results = release.execute_gate(
        evidence_path=evidence,
        junit_dir=junit_dir,
        which=_resolver(list(release.REQUIRED_TOOLS)),
        env=_default_env(),
        runner=runner,
        junit_parser=_junit_parser(tests=4),
    )

    assert exit_code == 0, [f.detail for r in results for f in r.findings]


def test_execute_gate_returns_blocked_without_postgres(tmp_path: Path) -> None:
    """The explicit BLOCKED path mandated by the brief.

    When ``POSTGRES_SMOKE_DATABASE_URL`` is unset, the gate must exit with
    code 2 (BLOCKED), never 0 (silent skip) or 1 (code failure). Evidence
    and dependency checks still run — the operator sees the full picture.
    """

    evidence = tmp_path / "evidence.md"
    _write_valid_evidence(evidence)
    junit_dir = tmp_path / "junit"
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        ("dependencies",): _completed(0),
        ("pip-audit",): _completed(0, stdout='{"dependencies": []}'),
        ("git", "ls-files"): _completed(0, stdout=""),
        ("git", "ls-files", ".artifacts"): _completed(0, stdout=""),
    }
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={junit_dir}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    exit_code, results = release.execute_gate(
        evidence_path=evidence,
        junit_dir=junit_dir,
        which=_resolver(list(release.REQUIRED_TOOLS)),
        env={},
        runner=runner,
        junit_parser=_junit_parser(tests=4),
    )

    assert exit_code == 2
    env_stage = next(r for r in results if r.stage == "environment")
    assert env_stage.blocked is True
    assert any(
        "POSTGRES_SMOKE_DATABASE_URL" in finding.detail
        for finding in env_stage.findings
    )


def test_execute_gate_prefers_failed_over_blocked(tmp_path: Path) -> None:
    """A code-level failure must mask a BLOCKED signal.

    When both a BLOCKED and a FAILED condition are present, the gate
    exits 1 so operators notice the actionable fix first.
    """

    evidence = tmp_path / "evidence.md"
    _write_valid_evidence(evidence)
    junit_dir = tmp_path / "junit"
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        ("dependencies",): _completed(1, stderr="broken"),
        ("pip-audit",): _completed(0, stdout='{"dependencies": []}'),
        ("git", "ls-files"): _completed(0, stdout=""),
        ("git", "ls-files", ".artifacts"): _completed(0, stdout=""),
    }
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={junit_dir}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    exit_code, _results = release.execute_gate(
        evidence_path=evidence,
        junit_dir=junit_dir,
        which=_resolver(list(release.REQUIRED_TOOLS)),
        env={},  # BLOCKED path, but dependency check also fails
        runner=runner,
        junit_parser=_junit_parser(tests=4),
    )

    assert exit_code == 1


def test_execute_gate_blocks_when_tool_missing(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.md"
    _write_valid_evidence(evidence)
    junit_dir = tmp_path / "junit"
    # Scan/artifacts stages still enumerate the tree even without poetry;
    # script them to pass so the only non-success signal is the missing
    # tool, which must surface as BLOCKED (exit 2).
    runner = ScriptedRunner(
        {
            ("git", "ls-files"): _completed(0, stdout=""),
            ("git", "ls-files", ".artifacts"): _completed(0, stdout=""),
        }
    )

    exit_code, _results = release.execute_gate(
        evidence_path=evidence,
        junit_dir=junit_dir,
        which=_resolver([]),
        env=_default_env(),
        runner=runner,
    )

    assert exit_code == 2


def test_execute_gate_fails_on_audit_advisory(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.md"
    _write_valid_evidence(evidence)
    junit_dir = tmp_path / "junit"
    runner_mapping: dict[tuple[str, ...], subprocess.CompletedProcess[str]] = {
        ("dependencies",): _completed(0),
        ("pip-audit",): _completed(
            1,
            stdout='{"dependencies": [{"name": "foo", "vulns": [{"id": "X"}]}]}',
        ),
        ("git", "ls-files"): _completed(0, stdout=""),
        ("git", "ls-files", ".artifacts"): _completed(0, stdout=""),
    }
    for name, _nodes in release.FOCUSED_TEST_SUITES:
        junit_arg = f"--junitxml={junit_dir}/{name}.xml"
        runner_mapping[("pytest", junit_arg)] = _completed(0)
    runner = ScriptedRunner(runner_mapping)

    exit_code, _results = release.execute_gate(
        evidence_path=evidence,
        junit_dir=junit_dir,
        which=_resolver(list(release.REQUIRED_TOOLS)),
        env=_default_env(),
        runner=runner,
        junit_parser=_junit_parser(tests=4),
    )

    assert exit_code == 1
