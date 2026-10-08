#!/usr/bin/env python3
"""Preflight gate for the media-assets-catalog rollout.

The release is fail-closed: every verification stage must succeed or the
script exits non-zero and no evidence is marked as approved. Operational
measurements that depend on external resources (PostgreSQL, backup fixture)
are not fabricated here — their absence is reported as BLOCKED and the
rollout decision stays pending until the evidence file is amended.

Exit codes:
    0  — every stage succeeded, rollout is cleared by this script
    1  — a stage failed (bad input, failing test, advisory, scan hit)
    2  — BLOCKED: an external prerequisite (env/tool/evidence) is missing

The gate also removes any JUnit output it created under ``--junit-dir`` so
that no release artifact ever gets tracked in the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Hook aliases keep the module boundary readable without pulling heavy
# typing machinery into every call site. The gate injects these hooks
# during tests to simulate subprocess, filesystem, and shutil.which
# without touching the real environment.
WhichHook = Callable[[str], "str | None"]
SubprocessHook = Callable[..., "subprocess.CompletedProcess[str]"]
FileReader = Callable[[Path], str]
JUnitParser = Callable[[Path], "dict[str, int]"]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# External tools whose presence is a hard precondition for the gate. The
# release cannot run without encrypted backup/restore, dependency audit, or
# the Python/pnpm runtimes that drive the focused suites.
REQUIRED_TOOLS: tuple[str, ...] = (
    "age",
    "age-keygen",
    "poetry",
    "pip-audit",
    "pnpm",
    "git",
)

# Environment variables that must be present. Each one, if unset, is a
# BLOCKED signal — not a code-level failure — because the measurement
# depends on an external PostgreSQL instance that only humans can provision.
REQUIRED_ENV: tuple[str, ...] = ("POSTGRES_SMOKE_DATABASE_URL",)

# Headings the evidence document must declare before the release can be
# cleared. The scanner also looks for the two measurement names so the
# rollout decision cannot be silently detached from numbers.
REQUIRED_EVIDENCE_SECTIONS: tuple[str, ...] = (
    "Commands",
    "Owner",
    "Environment",
    "Approvals",
    "Encryption",
    "Age key custody",
    "Owners and headroom",
    "Fixture measurements",
    "Decision",
)

# Phrases the evidence document must contain so that go/no-go is anchored
# to the objective capacity triggers from the wish.
REQUIRED_EVIDENCE_PHRASES: tuple[str, ...] = (
    "fixture",
    "backup",
    "restore",
    "DB",
    "WAL",
)

# Focused suites that must pass on the same tree before the release can be
# cleared. Each entry is a list of pytest node ids. The runner records the
# JUnit for every entry and rejects skips/xfailed in critical scenarios.
FOCUSED_TEST_SUITES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "dependencies-and-release",
        ("tests/scripts/test_check_media_dependencies.py",),
    ),
    (
        "backup-restore",
        ("tests/scripts/test_backup_encryption.py",),
    ),
    (
        "postgres-persistence",
        ("tests/integration/test_media_assets_postgres.py",),
    ),
    (
        "api-authorization-and-crud",
        (
            "tests/api/test_media_assets.py",
            "tests/api/test_media_assets_authorization.py",
            "tests/api/test_media_assets_upload.py",
            "tests/api/test_media_assets_openapi.py",
            "tests/api/test_billing_usage.py",
            "tests/api/test_health.py",
            "tests/services/test_tenant_classification.py",
            "tests/services/test_media_assets.py",
            "tests/services/test_billing_usage_service.py",
            "tests/config/test_media_settings.py",
            "tests/api/test_admin.py",
        ),
    ),
)

# Regular expressions used to catch plaintext media bytes or sensitive
# material that must never appear in tracked files or operator evidence.
# The patterns are intentionally conservative — the goal is to reject
# obvious leaks (age private identities, raw private key material, long
# base64 blobs, JWTs) rather than to replace a full secret scanner. The
# scan deliberately excludes generic password=... assignments because
# test fixtures legitimately contain such strings; the operator policy
# relies on code review for test-data choices and on this scanner for
# material that is unambiguously secret.
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "age private identity",
        re.compile(r"AGE-SECRET-KEY-1[0-9A-Z]{20,}"),
    ),
    (
        "openssh private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "jwt token",
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
    (
        "aws access key id",
        re.compile(r"AKIA[0-9A-Z]{16}"),
    ),
    (
        "slack token",
        re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    ),
)

# The gate scans the entire tracked tree for sensitive material. These
# directories contain fixture-only files (synthetic faker data, generated
# PT-BR corpora, cassette fixtures, synthetic secret strings embedded in
# tests that exercise the scanner itself). They remain subject to code
# review and are explicitly out of scope for the release scan.
SCAN_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "tests/fixtures/",
    "tests/baselines/",
    "tests/scripts/",
    "frontend/node_modules/",
    "node_modules/",
)


class ReleaseError(Exception):
    """Fatal problem detected by a release stage; exits non-zero."""


class ReleaseBlocked(Exception):
    """A precondition is missing — the release is BLOCKED, not failed."""


@dataclass
class Finding:
    """Structured message emitted by any stage."""

    severity: str
    stage: str
    detail: str


@dataclass
class StageResult:
    """Aggregate of findings produced by a single stage."""

    stage: str
    findings: list[Finding] = field(default_factory=list)
    blocked: bool = False
    failed: bool = False

    def add_error(self, detail: str) -> None:
        self.findings.append(Finding("error", self.stage, detail))
        self.failed = True

    def add_block(self, detail: str) -> None:
        self.findings.append(Finding("blocked", self.stage, detail))
        self.blocked = True

    def add_info(self, detail: str) -> None:
        self.findings.append(Finding("info", self.stage, detail))


def _which(name: str) -> str | None:
    return shutil.which(name)


def verify_tools(
    required: Sequence[str] = REQUIRED_TOOLS,
    which: WhichHook | None = None,
) -> StageResult:
    """Resolve every required tool on ``PATH``.

    A missing tool is a BLOCKED outcome: the gate cannot measure what it
    was asked to measure. The injectable ``which`` hook keeps unit tests
    deterministic without touching ``PATH``.
    """

    result = StageResult(stage="tools")
    resolver = which if which is not None else _which
    for name in required:
        location = resolver(name)
        if not location:
            result.add_block(f"required tool is missing from PATH: {name}")
        else:
            result.add_info(f"{name} resolved at {location}")
    return result


def verify_environment(
    required: Sequence[str] = REQUIRED_ENV,
    env: Mapping[str, str] | None = None,
) -> StageResult:
    """Fail-closed when any required environment variable is unset.

    The gate treats these variables as evidence of an operational target
    (database, backup destination). Missing values are BLOCKED, never
    silently skipped.
    """

    result = StageResult(stage="environment")
    environment = env if env is not None else os.environ
    for name in required:
        value = environment.get(name)
        if not value:
            result.add_block(f"required environment variable is not set: {name}")
        else:
            # The variable's content may be a secret URL. Only its presence
            # is reported — never its value.
            result.add_info(f"{name} is set")
    return result


def verify_evidence(
    path: Path,
    required_sections: Sequence[str] = REQUIRED_EVIDENCE_SECTIONS,
    required_phrases: Sequence[str] = REQUIRED_EVIDENCE_PHRASES,
) -> StageResult:
    """Enforce the operational evidence document contract.

    The document must exist, contain every required heading, and mention
    the measurement anchors so the go/no-go decision cannot be made in a
    vacuum. Absent evidence is BLOCKED; a tampered document that drops a
    required section is a FAIL — the operator published the file but did
    not supply the content it promised.
    """

    result = StageResult(stage="evidence")
    if not path.exists():
        result.add_block(f"evidence document is missing: {path}")
        return result
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        result.add_error(f"cannot read evidence document {path}: {exc}")
        return result
    if not content.strip():
        result.add_error(f"evidence document is empty: {path}")
        return result
    lower = content.lower()
    for heading in required_sections:
        marker = f"## {heading}".lower()
        if marker not in lower:
            # A missing heading is a structural fault in the document the
            # operator published — treat as failure, not BLOCKED.
            result.add_error(f"evidence document is missing section: {heading}")
    for phrase in required_phrases:
        if phrase.lower() not in lower:
            result.add_error(f"evidence document is missing anchor phrase: {phrase}")
    # Placeholder markers indicate the operator published the document but
    # did not supply the operational measurement yet. Per wish policy,
    # absence of external measurement is BLOCKED (exit 2), not a hard
    # failure. Operators receive the full list so they know what to fill.
    placeholder_markers = (
        "tbd",
        "to-be-decided",
        "to be decided",
        "fill with",
    )
    for marker in placeholder_markers:
        if marker in lower:
            result.add_block(
                f"evidence document still contains placeholder '{marker}'; "
                "operational measurement is pending"
            )
    return result


def _run_subprocess(
    cmd: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    runner: SubprocessHook | None = None,
) -> subprocess.CompletedProcess[str]:
    """Thin wrapper around :func:`subprocess.run` that is easy to replace.

    Tests inject a ``runner`` callable to simulate pip-audit, pytest, etc.,
    without executing them.
    """

    if runner is None:
        return subprocess.run(
            list(cmd),
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
    return runner(list(cmd), cwd=cwd, env=env)


def run_dependency_check(
    *,
    repository_root: Path = REPOSITORY_ROOT,
    runner: SubprocessHook | None = None,
) -> StageResult:
    """Run the existing media dependency checker as a subprocess."""

    result = StageResult(stage="dependencies")
    cmd = ("poetry", "run", "python", "scripts/check_media_dependencies.py")
    completed = _run_subprocess(cmd, cwd=repository_root, runner=runner)
    if completed.returncode != 0:
        result.add_error(
            f"check_media_dependencies.py exited {completed.returncode}: "
            f"{completed.stdout}{completed.stderr}"
        )
    else:
        result.add_info("media dependency check passed")
    return result


def run_pip_audit(
    *,
    repository_root: Path = REPOSITORY_ROOT,
    runner: SubprocessHook | None = None,
) -> StageResult:
    """Execute ``pip-audit --local`` and inspect its output for advisories.

    The subprocess returning non-zero already signals vulnerabilities,
    but the JSON output is parsed independently so the gate can flag
    advisories that for any reason returned 0 (a stub, a cached
    snapshot) and so a non-zero exit solely caused by the local-only
    project (``neurostack-api`` is not published to PyPI) is not treated
    as a vulnerability. Both failure modes — real advisory and
    unparseable payload — are tested explicitly.
    """

    result = StageResult(stage="pip-audit")
    cmd = (
        "poetry",
        "run",
        "pip-audit",
        "--local",
        "--format",
        "json",
    )
    completed = _run_subprocess(cmd, cwd=repository_root, runner=runner)
    advisory_count = _count_pip_audit_advisories(completed.stdout or "")
    stderr_text = (completed.stderr or "").strip()
    stdout_text = (completed.stdout or "").strip()
    local_only_noise = _is_local_only_noise(stderr_text)
    if completed.returncode != 0:
        # pip-audit treats "package not on PyPI" as a non-zero exit when
        # auditing a local project that is not published. If the only
        # cause is the local project itself and no advisory is in the
        # JSON output, the gate accepts the audit as successful. Any
        # other failure is reported.
        if local_only_noise and advisory_count == 0:
            result.add_info("pip-audit --local ignored local-only package metadata")
        else:
            result.add_error(
                "pip-audit --local reported failure "
                f"(exit {completed.returncode}): "
                f"{stdout_text[:400]} {stderr_text[:400]}"
            )
    if advisory_count is None:
        # Non-JSON output on a zero-exit run is suspicious — the gate
        # cannot prove the audit happened. Report it as a failure.
        if completed.returncode == 0:
            result.add_error(
                "pip-audit output could not be parsed as JSON; cannot "
                "prove the audit executed"
            )
    elif advisory_count > 0:
        result.add_error(
            f"pip-audit reported {advisory_count} advisory/advisories "
            "that must be remediated before release"
        )
    if not result.failed:
        result.add_info("pip-audit --local returned no advisories")
    return result


def _is_local_only_noise(stderr_text: str) -> bool:
    """True when stderr only mentions the local project missing from PyPI.

    pip-audit emits
    ``neurostack-api: Dependency not found on PyPI and could not be audited``
    whenever a local project is not published; the gate must not mistake
    that for a vulnerability. We require every stderr line to be either
    empty or match the known noise shape.
    """

    for line in stderr_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "Dependency not found on PyPI and could not be audited" in stripped:
            continue
        return False
    return True


def _count_pip_audit_advisories(stdout: str) -> int | None:
    """Return the number of vulnerabilities in a pip-audit JSON payload.

    ``None`` indicates the payload is not JSON (unknown shape).
    """

    stripped = stdout.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    advisories = 0
    # pip-audit JSON shape: {"dependencies": [{"name": ..., "vulns": [...]}]}
    if isinstance(payload, dict):
        dependencies = payload.get("dependencies", [])
    else:
        dependencies = payload if isinstance(payload, list) else []
    for item in dependencies if isinstance(dependencies, list) else []:
        if not isinstance(item, dict):
            continue
        vulns = item.get("vulns") or item.get("vulnerabilities") or []
        if isinstance(vulns, list):
            advisories += len(vulns)
    return advisories


def run_focused_tests(
    *,
    junit_dir: Path,
    suites: Sequence[tuple[str, Sequence[str]]] = FOCUSED_TEST_SUITES,
    repository_root: Path = REPOSITORY_ROOT,
    runner: SubprocessHook | None = None,
    junit_parser: JUnitParser | None = None,
) -> StageResult:
    """Run each focused suite with ``--junitxml`` under ``junit_dir``.

    The runner rejects any skip/xfailed result because the policy forbids
    waivers for critical scenarios. JUnit files are parsed to count
    failures/errors/skips/xfails and the directory is wiped at the end
    (success or failure) so no artifact survives inside the repository.
    """

    result = StageResult(stage="tests")
    if not junit_dir.is_absolute():
        junit_dir = (repository_root / junit_dir).resolve()
    tracked_root = repository_root
    # Ensure the directory exists and is empty before the first suite runs.
    if junit_dir.exists():
        shutil.rmtree(junit_dir)
    junit_dir.mkdir(parents=True, exist_ok=True)
    try:
        for name, nodes in suites:
            junit_path = junit_dir / f"{name}.xml"
            cmd: tuple[str, ...] = (
                "poetry",
                "run",
                "pytest",
                "-q",
                f"--junitxml={junit_path}",
                *nodes,
            )
            completed = _run_subprocess(cmd, cwd=tracked_root, runner=runner)
            if completed.returncode != 0:
                result.add_error(
                    f"suite '{name}' pytest exited {completed.returncode}: "
                    f"{(completed.stdout or '').strip()[-400:]} "
                    f"{(completed.stderr or '').strip()[-400:]}"
                )
            try:
                counts = _parse_junit(junit_path, parser=junit_parser)
            except ReleaseError as exc:
                result.add_error(f"suite '{name}' JUnit invalid: {exc}")
                continue
            if counts["failures"] > 0 or counts["errors"] > 0:
                result.add_error(
                    f"suite '{name}' recorded failures={counts['failures']}"
                    f" errors={counts['errors']}"
                )
            if counts["skipped"] > 0:
                result.add_error(
                    f"suite '{name}' recorded {counts['skipped']} skip(s); "
                    "critical scenarios cannot be skipped"
                )
            if counts["xfailed"] > 0:
                result.add_error(
                    f"suite '{name}' recorded {counts['xfailed']} xfail(ed) "
                    "case(s); critical scenarios cannot be xfailed"
                )
            if counts["tests"] <= 0:
                result.add_error(f"suite '{name}' did not record any test case")
    finally:
        # The directory must always be removed, even if a stage raised.
        if junit_dir.exists():
            shutil.rmtree(junit_dir, ignore_errors=True)
    if junit_dir.exists():
        result.add_error(f"could not remove JUnit directory after run: {junit_dir}")
    return result


def _parse_junit(path: Path, *, parser: JUnitParser | None = None) -> dict[str, int]:
    """Return pass/fail/skip/xfail counts for ``path``.

    ``parser`` lets unit tests inject a canned result instead of touching
    the filesystem. The parser must raise :class:`ReleaseError` for a
    missing or malformed document; silent zeros would otherwise mask a
    corrupt test outcome.
    """

    if parser is not None:
        return parser(path)
    if not path.exists():
        raise ReleaseError(f"JUnit file is missing: {path}")
    try:
        tree = ElementTree.parse(path)
    except ElementTree.ParseError as exc:
        raise ReleaseError(f"JUnit file is malformed: {exc}") from exc
    root = tree.getroot()
    tests = failures = errors = skipped = xfailed = 0
    # pytest emits either <testsuites><testsuite>...</testsuite></testsuites>
    # or a bare <testsuite>; iterate the testsuite elements.
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    for suite in suites:
        tests += int(suite.attrib.get("tests", 0) or 0)
        failures += int(suite.attrib.get("failures", 0) or 0)
        errors += int(suite.attrib.get("errors", 0) or 0)
        skipped += int(suite.attrib.get("skipped", 0) or 0)
        for case in suite.findall("testcase"):
            for child in case:
                if child.tag != "skipped":
                    continue
                skip_type = (child.attrib.get("type") or "").lower()
                skip_message = (child.attrib.get("message") or "").lower()
                if "xfail" in skip_type or "xfail" in skip_message:
                    xfailed += 1
    return {
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "xfailed": xfailed,
    }


def scan_tracked_tree(
    *,
    repository_root: Path = REPOSITORY_ROOT,
    extra_paths: Iterable[Path] = (),
    patterns: Sequence[tuple[str, re.Pattern[str]]] = SENSITIVE_PATTERNS,
    excluded_prefixes: Sequence[str] = SCAN_EXCLUDED_PREFIXES,
    runner: SubprocessHook | None = None,
    reader: FileReader | None = None,
) -> StageResult:
    """Scan tracked files and operator evidence for sensitive data.

    ``git ls-files`` enumerates files under version control. Each file
    is read and every pattern is applied; a match is a fatal finding
    because the catalog's threat model forbids plaintext private keys,
    age identities, or long tokens from entering the repository or the
    operator evidence. ``extra_paths`` include evidence logs living
    outside git. ``excluded_prefixes`` keeps synthetic fixture corpora
    out of the scan: those files are allowed to contain plausible
    secrets by construction and are instead reviewed by maintainers.
    """

    result = StageResult(stage="scan")
    cmd = ("git", "ls-files")
    completed = _run_subprocess(cmd, cwd=repository_root, runner=runner)
    if completed.returncode != 0:
        result.add_error(
            f"git ls-files failed (exit {completed.returncode}): "
            f"{completed.stderr or ''}"
        )
        return result
    tracked_lines = [
        line.strip() for line in (completed.stdout or "").splitlines() if line.strip()
    ]
    file_reader = reader if reader is not None else _default_reader
    # Prefix-based exclusion is applied against the repo-relative path
    # (not the absolute one) because the configuration lists tree-level
    # paths. Extra paths bypass the prefix filter: operator evidence is
    # always scanned.
    tracked_paths: list[tuple[Path, str]] = []
    for rel in tracked_lines:
        if any(rel.startswith(prefix) for prefix in excluded_prefixes):
            continue
        tracked_paths.append((repository_root / rel, rel))
    extra_items: list[tuple[Path, str]] = [(path, str(path)) for path in extra_paths]
    skip_suffixes = (
        ".png",
        ".jpg",
        ".jpeg",
        ".pdf",
        ".mp3",
        ".mp4",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".zip",
        ".gz",
        ".age",
        ".tar",
        ".lock",
    )
    # Any file tracked under docs/evidence/ with a .xml suffix would mean
    # a JUnit survived the previous run. The runner already removes it,
    # but if a prior failure left debris it must still be surfaced here
    # rather than being masked by the extension filter.
    for path, label in [*tracked_paths, *extra_items]:
        if not path.exists() or not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in skip_suffixes:
            continue
        try:
            content = file_reader(path)
        except OSError as exc:
            result.add_error(f"cannot read {label}: {exc}")
            continue
        for pattern_label, pattern in patterns:
            if pattern.search(content):
                result.add_error(
                    f"sensitive pattern '{pattern_label}' matched in {label}"
                )
    return result


def _default_reader(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def ensure_release_artifacts_ignored(
    *,
    repository_root: Path = REPOSITORY_ROOT,
    runner: SubprocessHook | None = None,
) -> StageResult:
    """Verify no release artifact leaked into git tracking.

    The runner removes ``.artifacts/`` by itself, but the gate also calls
    ``git ls-files .artifacts`` to prove nothing accidentally entered the
    index — even on a previous failed run.
    """

    result = StageResult(stage="artifacts-tracking")
    cmd = ("git", "ls-files", ".artifacts")
    completed = _run_subprocess(cmd, cwd=repository_root, runner=runner)
    if completed.returncode != 0:
        result.add_error(
            f"git ls-files .artifacts failed (exit {completed.returncode}): "
            f"{completed.stderr or ''}"
        )
        return result
    tracked = [
        line.strip() for line in (completed.stdout or "").splitlines() if line.strip()
    ]
    if tracked:
        result.add_error(
            "release artifacts are tracked by git but must not be: " f"{tracked}"
        )
    else:
        result.add_info(".artifacts/ is not tracked by git")
    return result


def _render(results: Sequence[StageResult]) -> str:
    lines: list[str] = []
    for stage in results:
        for finding in stage.findings:
            lines.append(
                f"[{finding.severity.upper():7}] {finding.stage}: " f"{finding.detail}"
            )
    return "\n".join(lines)


def execute_gate(
    *,
    evidence_path: Path,
    junit_dir: Path,
    repository_root: Path = REPOSITORY_ROOT,
    which: WhichHook | None = None,
    env: Mapping[str, str] | None = None,
    runner: SubprocessHook | None = None,
    reader: FileReader | None = None,
    junit_parser: JUnitParser | None = None,
    tool_list: Sequence[str] = REQUIRED_TOOLS,
    env_list: Sequence[str] = REQUIRED_ENV,
) -> tuple[int, list[StageResult]]:
    """Run every stage and return ``(exit_code, results)``.

    Exit codes mirror the module docstring. Stages that would be
    structurally meaningless when a precondition is blocked (for example
    running the focused suites against a disposable PostgreSQL that is
    not provisioned) are themselves recorded as BLOCKED instead of
    being executed and producing noisy collection errors — a BLOCKED
    precondition must not escalate to a FAILED verdict.
    """

    results: list[StageResult] = []
    tool_result = verify_tools(required=tool_list, which=which)
    results.append(tool_result)
    env_result = verify_environment(required=env_list, env=env)
    results.append(env_result)
    evidence_result = verify_evidence(evidence_path)
    results.append(evidence_result)
    poetry_available = (which if which is not None else _which)("poetry") is not None
    if poetry_available:
        results.append(
            run_dependency_check(repository_root=repository_root, runner=runner)
        )
        results.append(run_pip_audit(repository_root=repository_root, runner=runner))
    else:
        skipped = StageResult(stage="dependencies")
        skipped.add_block("dependency check skipped because 'poetry' is not available")
        results.append(skipped)
        audit_skipped = StageResult(stage="pip-audit")
        audit_skipped.add_block("pip-audit skipped because 'poetry' is not available")
        results.append(audit_skipped)
    # The focused suites exercise PostgreSQL, backup/restore, auth,
    # concurrency, billing, OpenAPI. Each suite requires both poetry and
    # the operational database. If either precondition is BLOCKED, do
    # not execute pytest — the resulting collection errors would mask
    # the actual blocker as a FAILED stage.
    env_blocked = env_result.blocked
    if poetry_available and not env_blocked:
        results.append(
            run_focused_tests(
                junit_dir=junit_dir,
                repository_root=repository_root,
                runner=runner,
                junit_parser=junit_parser,
            )
        )
    else:
        tests_skipped = StageResult(stage="tests")
        if not poetry_available:
            tests_skipped.add_block(
                "focused suites skipped because 'poetry' is not available"
            )
        if env_blocked:
            tests_skipped.add_block(
                "focused suites skipped because operational environment "
                "is BLOCKED; running them would mask the blocker as a "
                "test failure"
            )
        results.append(tests_skipped)
    results.append(
        scan_tracked_tree(
            repository_root=repository_root,
            extra_paths=(evidence_path,),
            runner=runner,
            reader=reader,
        )
    )
    results.append(
        ensure_release_artifacts_ignored(repository_root=repository_root, runner=runner)
    )
    exit_code = _derive_exit_code(results)
    return exit_code, results


def _derive_exit_code(results: Sequence[StageResult]) -> int:
    if any(stage.failed for stage in results):
        return 1
    if any(stage.blocked for stage in results):
        return 2
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed preflight gate for the media-assets release."
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        required=True,
        help="Path to the operational evidence markdown document.",
    )
    parser.add_argument(
        "--junit-dir",
        type=Path,
        required=True,
        help="Directory under which temporary JUnit XML files are stored.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    evidence_path = args.evidence
    if not evidence_path.is_absolute():
        evidence_path = (Path.cwd() / evidence_path).resolve()
    junit_dir = args.junit_dir
    if not junit_dir.is_absolute():
        junit_dir = (Path.cwd() / junit_dir).resolve()
    exit_code, results = execute_gate(evidence_path=evidence_path, junit_dir=junit_dir)
    rendered = _render(results)
    stream = sys.stderr if exit_code != 0 else sys.stdout
    if rendered:
        print(rendered, file=stream)
    if exit_code == 0:
        print("media release preflight passed", file=sys.stdout)
    elif exit_code == 2:
        print(
            "media release preflight BLOCKED: operational evidence not " "available",
            file=sys.stderr,
        )
    else:
        print(
            "media release preflight FAILED: see findings above",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
