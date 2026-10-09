#!/usr/bin/env python3
"""Fail-closed release runner for the Central de Clonagem selective clone.

The runner executes one extensible manifest of focused pytest files against a
real PostgreSQL (``POSTGRES_SMOKE_DATABASE_URL``) with a JUnit report written
under an ignored ``.artifacts/`` directory, then proves from that report that
every manifest file ran with no failure, error, skip or xfail. The temporary
directory is always removed, on success, on every error path and on SIGTERM.

Exit codes (same contract as ``scripts/check_media_release.py``):
    0  — every stage succeeded
    1  — a stage failed (bad argument, failing pytest, invalid JUnit, ...)
    2  — BLOCKED: an external prerequisite (environment/tool) is missing
    143 — terminated by SIGTERM (the temporary directory is still removed)
"""

# Standalone on purpose: scripts are loaded by path and cannot import each
# other cleanly, and this gate is stricter than check_media_release.py (confined
# .artifacts/ directory, refuses a pre-existing one, pytest timeout, no
# evidence/pip-audit stages).

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import NoReturn

# Hooks injected by tests to simulate pytest, git and PATH lookups.
WhichHook = Callable[[str], "str | None"]
SubprocessHook = Callable[..., "subprocess.CompletedProcess[str]"]

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIRNAME = ".artifacts"
DEFAULT_JUNIT_DIR = Path(ARTIFACTS_DIRNAME) / "clinic-clone-release"
JUNIT_FILENAME = "clinic-clone-release.xml"
PYTEST_TIMEOUT_SECONDS = 1800
SIGTERM_EXIT_CODE = 143

REQUIRED_TOOLS: tuple[str, ...] = ("poetry", "git")
REQUIRED_ENV: tuple[str, ...] = ("POSTGRES_SMOKE_DATABASE_URL",)

# Focused pytest files that must all pass on the same tree. Every scenario is
# critical: a skip or xfail anywhere in the manifest fails the release. The
# media capacity suites come first, then the selective clone suites that
# consume the same storage guard.
CLINIC_CLONE_RELEASE_MANIFEST: list[str] = [
    "tests/services/test_media_assets.py",
    "tests/api/test_media_assets_upload.py",
    "tests/integration/test_media_assets_postgres.py",
    "tests/services/test_clinic_clone_service.py",
    "tests/api/test_builder_cloning.py",
    "tests/integration/test_builder_cloning_postgres.py",
]


class ReleaseError(Exception):
    """Fatal problem detected by a stage; exits non-zero."""


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


@dataclass(frozen=True)
class JUnitCounts:
    """Outcome totals proven from one pytest JUnit report."""

    tests: int
    failures: int
    errors: int
    skipped: int
    xfailed: int
    cases_by_file: Mapping[str, int]


def _run_subprocess(
    cmd: Sequence[str],
    *,
    cwd: Path,
    runner: SubprocessHook | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` capturing text output; tests inject ``runner`` instead."""

    if runner is None:
        return subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    return runner(list(cmd), cwd=cwd, timeout=timeout)


def verify_tools(
    required: Sequence[str] = REQUIRED_TOOLS,
    which: WhichHook | None = None,
) -> StageResult:
    """A missing tool is BLOCKED: the runner cannot measure anything."""

    result = StageResult(stage="tools")
    resolver = which if which is not None else shutil.which
    for name in required:
        if resolver(name):
            result.add_info(f"{name} is available")
        else:
            result.add_block(f"required tool is missing from PATH: {name}")
    return result


def verify_environment(
    required: Sequence[str] = REQUIRED_ENV,
    env: Mapping[str, str] | None = None,
) -> StageResult:
    """Missing or blank variables are BLOCKED; values are never reported."""

    result = StageResult(stage="environment")
    environment = env if env is not None else os.environ
    for name in required:
        if (environment.get(name) or "").strip():
            result.add_info(f"{name} is set")
        else:
            result.add_block(f"required environment variable is not set: {name}")
    return result


def resolve_junit_dir(
    junit_dir: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
) -> Path:
    """Confine the temporary directory to a child of ``<repo>/.artifacts``.

    The runner deletes this directory unconditionally, so it must never point
    at the repository, ``.artifacts`` itself or anything outside it.
    """

    root = repository_root.resolve()
    candidate = junit_dir if junit_dir.is_absolute() else root / junit_dir
    resolved = candidate.resolve()
    artifacts_root = root / ARTIFACTS_DIRNAME
    if resolved == artifacts_root or artifacts_root not in resolved.parents:
        raise ReleaseError(
            f"--junit-dir must be a directory inside {ARTIFACTS_DIRNAME}/: "
            f"{junit_dir}"
        )
    return resolved


def _manifest_module(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".")


def _int_attribute(element: ElementTree.Element, name: str) -> int:
    raw = element.attrib.get(name, "0")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReleaseError(
            f"JUnit file is malformed: attribute {name}={raw!r} is not an integer"
        ) from exc
    if value < 0:
        raise ReleaseError(f"JUnit file is malformed: attribute {name} is negative")
    return value


def parse_junit(path: Path, manifest: Sequence[str]) -> JUnitCounts:
    """Parse a pytest JUnit report or raise :class:`ReleaseError`.

    Totals come from both the suite attributes and the individual test cases,
    and the larger of the two wins, so a report whose attributes understate
    its own cases still fails closed.
    """

    if not path.is_file():
        raise ReleaseError(f"JUnit file is missing: {path.name}")
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise ReleaseError(f"JUnit file is malformed: {exc}") from exc
    if root.tag == "testsuites":
        suites = root.findall("testsuite")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise ReleaseError(f"JUnit file is malformed: unexpected root <{root.tag}>")
    if not suites:
        raise ReleaseError("JUnit file is malformed: no <testsuite> element")

    modules = {_manifest_module(entry): entry for entry in manifest}
    cases_by_file = {entry: 0 for entry in manifest}
    tests = failures = errors = skipped = 0
    case_failures = case_errors = case_skipped = xfailed = case_count = 0
    for suite in suites:
        tests += _int_attribute(suite, "tests")
        failures += _int_attribute(suite, "failures")
        errors += _int_attribute(suite, "errors")
        skipped += _int_attribute(suite, "skipped")
        for case in suite.iter("testcase"):
            case_count += 1
            classname = case.attrib.get("classname", "")
            for module, entry in modules.items():
                if classname == module or classname.startswith(f"{module}."):
                    cases_by_file[entry] += 1
                    break
            for child in case:
                if child.tag == "failure":
                    case_failures += 1
                elif child.tag == "error":
                    case_errors += 1
                elif child.tag == "skipped":
                    case_skipped += 1
                    marker = (
                        f"{child.attrib.get('type', '')} "
                        f"{child.attrib.get('message', '')}"
                    ).lower()
                    if "xfail" in marker:
                        xfailed += 1
    return JUnitCounts(
        tests=max(tests, case_count),
        failures=max(failures, case_failures),
        errors=max(errors, case_errors),
        skipped=max(skipped, case_skipped),
        xfailed=xfailed,
        cases_by_file=cases_by_file,
    )


def run_manifest(
    *,
    junit_dir: Path,
    manifest: Sequence[str] = CLINIC_CLONE_RELEASE_MANIFEST,
    repository_root: Path = REPOSITORY_ROOT,
    runner: SubprocessHook | None = None,
    timeout: float = PYTEST_TIMEOUT_SECONDS,
) -> StageResult:
    """Run the manifest once with JUnit and always remove ``junit_dir``.

    ``junit_dir`` must already be confined by :func:`resolve_junit_dir`. A
    pre-existing directory is refused instead of deleted: the runner only
    ever removes the temporary directory it created itself.
    """

    result = StageResult(stage="tests")
    if not manifest:
        result.add_error("release manifest is empty")
        return result
    missing = [entry for entry in manifest if not (repository_root / entry).is_file()]
    if missing:
        result.add_error(f"manifest files are missing: {missing}")
        return result
    if junit_dir.exists():
        result.add_error(
            "JUnit directory already exists and was not created by this run; "
            f"remove it and retry: {junit_dir}"
        )
        return result

    junit_dir.mkdir(parents=True)
    try:
        junit_path = junit_dir / JUNIT_FILENAME
        cmd = (
            "poetry",
            "run",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "xfail_strict=true",
            f"--junitxml={junit_path}",
            *manifest,
        )
        try:
            completed = _run_subprocess(
                cmd,
                cwd=repository_root,
                runner=runner,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            result.add_error(f"pytest exceeded the {timeout:g}s timeout")
            return result
        output = f"{completed.stdout or ''}{completed.stderr or ''}".strip()
        if completed.returncode != 0:
            result.add_error(f"pytest exited {completed.returncode}: {output[-1200:]}")
        try:
            counts = parse_junit(junit_path, manifest)
        except ReleaseError as exc:
            result.add_error(str(exc))
            return result
        if counts.tests <= 0:
            result.add_error("JUnit did not record any test case")
        if counts.failures or counts.errors:
            result.add_error(
                f"JUnit recorded failures={counts.failures} errors={counts.errors}"
            )
        if counts.skipped:
            result.add_error(
                f"JUnit recorded {counts.skipped} skip(s); critical scenarios "
                "cannot be skipped"
            )
        if counts.xfailed:
            result.add_error(
                f"JUnit recorded {counts.xfailed} xfail(ed) case(s); critical "
                "scenarios cannot be xfailed"
            )
        for entry, cases in counts.cases_by_file.items():
            if cases <= 0:
                result.add_error(f"JUnit recorded no test case for {entry}")
        if not result.failed:
            result.add_info(
                f"{counts.tests} test case(s) passed across {len(manifest)} file(s)"
            )
    finally:
        shutil.rmtree(junit_dir, ignore_errors=True)
        if junit_dir.exists():
            result.add_error(f"could not remove JUnit directory: {junit_dir}")
    return result


def ensure_artifacts_untracked(
    *,
    repository_root: Path = REPOSITORY_ROOT,
    runner: SubprocessHook | None = None,
) -> StageResult:
    """Prove no release artifact entered the git index."""

    result = StageResult(stage="artifacts-tracking")
    completed = _run_subprocess(
        ("git", "ls-files", ARTIFACTS_DIRNAME),
        cwd=repository_root,
        runner=runner,
    )
    if completed.returncode != 0:
        result.add_error(
            f"git ls-files {ARTIFACTS_DIRNAME} failed "
            f"(exit {completed.returncode}): {completed.stderr or ''}"
        )
        return result
    tracked = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if tracked:
        result.add_error(f"release artifacts are tracked by git: {tracked}")
    else:
        result.add_info(f"{ARTIFACTS_DIRNAME}/ is not tracked by git")
    return result


def execute_gate(
    *,
    junit_dir: Path,
    repository_root: Path = REPOSITORY_ROOT,
    manifest: Sequence[str] = CLINIC_CLONE_RELEASE_MANIFEST,
    which: WhichHook | None = None,
    env: Mapping[str, str] | None = None,
    runner: SubprocessHook | None = None,
) -> tuple[int, list[StageResult]]:
    """Run every stage and return ``(exit_code, results)``.

    pytest never runs while a precondition is BLOCKED or the JUnit directory
    is invalid, so a missing database can never surface as a test failure or,
    worse, as a pass.
    """

    results: list[StageResult] = []
    location = StageResult(stage="junit-dir")
    try:
        resolved_dir = resolve_junit_dir(junit_dir, repository_root=repository_root)
    except ReleaseError as exc:
        location.add_error(str(exc))
        return 1, [location]
    location.add_info(f"JUnit directory: {resolved_dir}")
    results.append(location)

    tools = verify_tools(which=which)
    environment = verify_environment(env=env)
    results.extend([tools, environment])
    if tools.blocked or environment.blocked:
        not_run = StageResult(stage="tests")
        not_run.add_block(
            "focused suites were not run because a prerequisite is missing"
        )
        results.append(not_run)
    else:
        results.append(
            run_manifest(
                junit_dir=resolved_dir,
                manifest=manifest,
                repository_root=repository_root,
                runner=runner,
            )
        )
        results.append(
            ensure_artifacts_untracked(repository_root=repository_root, runner=runner)
        )
    return _derive_exit_code(results), results


def _derive_exit_code(results: Sequence[StageResult]) -> int:
    if any(stage.failed for stage in results):
        return 1
    if any(stage.blocked for stage in results):
        return 2
    return 0


def _render(results: Sequence[StageResult]) -> str:
    return "\n".join(
        f"[{finding.severity.upper():7}] {finding.stage}: {finding.detail}"
        for stage in results
        for finding in stage.findings
    )


class _UsageErrorParser(argparse.ArgumentParser):
    """Report usage errors as failures (exit 1); exit 2 is reserved for BLOCKED."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(1)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = _UsageErrorParser(
        description="Fail-closed release runner for the clinic clone manifest."
    )
    parser.add_argument(
        "--junit-dir",
        type=Path,
        default=DEFAULT_JUNIT_DIR,
        help=(
            "Temporary JUnit directory inside .artifacts/; it must not exist "
            "and is always removed."
        ),
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _raise_on_sigterm(signum: int, frame: FrameType | None) -> NoReturn:
    # Python's default SIGTERM disposition exits without unwinding, which would
    # skip the ``finally`` that removes the temporary directory.
    raise SystemExit(SIGTERM_EXIT_CODE)


def main(argv: Sequence[str] | None = None) -> int:
    previous = signal.signal(signal.SIGTERM, _raise_on_sigterm)
    try:
        return _main(argv)
    finally:
        signal.signal(
            signal.SIGTERM,
            signal.SIG_DFL if previous is None else previous,
        )


def _main(argv: Sequence[str] | None) -> int:
    args = parse_args(argv)
    exit_code, results = execute_gate(junit_dir=args.junit_dir)
    stream = sys.stdout if exit_code == 0 else sys.stderr
    rendered = _render(results)
    if rendered:
        print(rendered, file=stream)
    if exit_code == 0:
        print("clinic clone release check passed", file=sys.stdout)
    elif exit_code == 2:
        print("clinic clone release check BLOCKED", file=sys.stderr)
    else:
        print("clinic clone release check FAILED", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
