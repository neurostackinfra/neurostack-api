"""Planted-failure tests for the clinic clone release runner.

Each test plants exactly one failure the policy forbids (missing database URL,
non-zero pytest, skip/xfail, missing or malformed JUnit, ...) and proves the
runner refuses to pass and never leaves its temporary directory behind.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from uuid import uuid4

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts/check_clinic_clone_release.py"
SPEC = importlib.util.spec_from_file_location("check_clinic_clone_release", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
# @dataclass resolves the owning module through sys.modules.
sys.modules["check_clinic_clone_release"] = release
SPEC.loader.exec_module(release)

MEDIA_MANIFEST = (
    "tests/services/test_media_assets.py",
    "tests/api/test_media_assets_upload.py",
    "tests/integration/test_media_assets_postgres.py",
)
CLONE_MANIFEST = (
    "tests/services/test_clinic_clone_service.py",
    "tests/api/test_builder_cloning.py",
    "tests/integration/test_builder_cloning_postgres.py",
)
MANIFEST = (*MEDIA_MANIFEST, *CLONE_MANIFEST)
ENV = {"POSTGRES_SMOKE_DATABASE_URL": "postgresql://smoke@127.0.0.1/postgres"}
JUNIT_DIR = Path(".artifacts/clinic-clone-release")


def _which(name: str) -> str:
    return f"/usr/bin/{name}"


def _testcase(module: str, name: str, body: str = "") -> str:
    return f'<testcase classname="{module}" name="{name}" time="0.01">{body}</testcase>'


def _junit(
    *,
    cases: Sequence[str] | None = None,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
) -> str:
    if cases is None:
        cases = [
            _testcase(entry.removesuffix(".py").replace("/", "."), f"test_{index}")
            for index, entry in enumerate(MANIFEST)
        ]
    return (
        '<?xml version="1.0" encoding="utf-8"?><testsuites>'
        f'<testsuite name="pytest" errors="{errors}" failures="{failures}" '
        f'skipped="{skipped}" tests="{len(cases)}" time="0.1">'
        f'{"".join(cases)}</testsuite></testsuites>'
    )


class FakePytest:
    """Subprocess hook that plays pytest and ``git ls-files``."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        junit: str | None = None,
        tracked: str = "",
        raises: BaseException | None = None,
        observe: Callable[[Path], None] | None = None,
    ) -> None:
        self.returncode = returncode
        self.junit = _junit() if junit is None else junit
        self.tracked = tracked
        self.raises = raises
        self.observe = observe
        self.calls: list[tuple[str, ...]] = []
        self.pytest_timeouts: list[float | None] = []
        self.junit_dir_seen: Path | None = None

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(cmd)
        self.calls.append(command)
        if command[:2] == ("git", "ls-files"):
            return subprocess.CompletedProcess(list(cmd), 0, self.tracked, "")
        assert command[:3] == ("poetry", "run", "pytest")
        self.pytest_timeouts.append(timeout)
        junit_token = next(
            token for token in command if token.startswith("--junitxml=")
        )
        junit_path = Path(junit_token.removeprefix("--junitxml="))
        self.junit_dir_seen = junit_path.parent
        assert junit_path.parent.is_dir()
        if self.observe is not None:
            self.observe(junit_path)
        if self.raises is not None:
            raise self.raises
        if self.junit != "<absent>":
            junit_path.write_text(self.junit, encoding="utf-8")
        return subprocess.CompletedProcess(list(cmd), self.returncode, "1 passed", "")

    @property
    def pytest_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[:3] == ("poetry", "run", "pytest")]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for entry in MANIFEST:
        target = tmp_path / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
    return tmp_path


def _gate(
    repo: Path,
    runner: FakePytest,
    *,
    env: Mapping[str, str] = ENV,
    junit_dir: Path = JUNIT_DIR,
    which: Callable[[str], str | None] = _which,
) -> tuple[int, list[object]]:
    exit_code, results = release.execute_gate(
        junit_dir=junit_dir,
        repository_root=repo,
        manifest=MANIFEST,
        which=which,
        env=env,
        runner=runner,
    )
    return int(exit_code), list(results)


def _details(results: Sequence[object]) -> str:
    return "\n".join(
        finding.detail
        for stage in results
        for finding in stage.findings  # type: ignore[attr-defined]
    )


def _module(entry: str) -> str:
    return entry.removesuffix(".py").replace("/", ".")


def _cases_except(
    excluded: str | None = None,
    *,
    body_for: str | None = None,
    body: str = "",
) -> list[str]:
    return [
        _testcase(_module(entry), "test_case", body if entry == body_for else "")
        for entry in MANIFEST
        if entry != excluded
    ]


def test_manifest_keeps_media_suites_and_adds_every_clone_suite() -> None:
    assert isinstance(release.CLINIC_CLONE_RELEASE_MANIFEST, list)
    assert release.CLINIC_CLONE_RELEASE_MANIFEST == list(MANIFEST)
    assert release.CLINIC_CLONE_RELEASE_MANIFEST[: len(MEDIA_MANIFEST)] == list(
        MEDIA_MANIFEST
    )
    assert len(set(release.CLINIC_CLONE_RELEASE_MANIFEST)) == len(MANIFEST)
    for entry in release.CLINIC_CLONE_RELEASE_MANIFEST:
        assert (REPOSITORY_ROOT / entry).is_file()


def test_default_gate_runs_the_full_manifest_in_one_pytest_invocation(
    repo: Path,
) -> None:
    runner = FakePytest()

    exit_code, results = release.execute_gate(
        junit_dir=JUNIT_DIR,
        repository_root=repo,
        which=_which,
        env=ENV,
        runner=runner,
    )

    assert exit_code == 0, _details(results)
    assert len(runner.pytest_calls) == 1
    assert runner.pytest_calls[0][-len(MANIFEST) :] == MANIFEST
    assert f"{len(MANIFEST)} file(s)" in _details(results)
    assert not (repo / JUNIT_DIR).exists()


@pytest.mark.parametrize("entry", CLONE_MANIFEST)
def test_clone_suite_without_recorded_cases_fails_and_cleans_up(
    repo: Path,
    entry: str,
) -> None:
    runner = FakePytest(junit=_junit(cases=_cases_except(entry)))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert f"JUnit recorded no test case for {entry}" in _details(results)
    assert not (repo / JUNIT_DIR).exists()


@pytest.mark.parametrize("entry", CLONE_MANIFEST)
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<skipped type="pytest.skip" message="no database">skip</skipped>', "skip"),
        ('<skipped type="pytest.xfail" message="known">xfail</skipped>', "xfail"),
        ('<failure message="boom">trace</failure>', "failures=1"),
        ('<error message="fixture">trace</error>', "errors=1"),
    ],
)
def test_critical_outcome_in_any_clone_suite_fails_and_cleans_up(
    repo: Path,
    entry: str,
    body: str,
    expected: str,
) -> None:
    runner = FakePytest(junit=_junit(cases=_cases_except(body_for=entry, body=body)))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert expected in _details(results)
    assert not (repo / JUNIT_DIR).exists()


@pytest.mark.parametrize("entry", CLONE_MANIFEST)
def test_missing_clone_suite_file_fails_before_pytest(repo: Path, entry: str) -> None:
    (repo / entry).unlink()
    runner = FakePytest()

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert runner.pytest_calls == []
    assert entry in _details(results)
    assert not (repo / ".artifacts").exists()


def test_nonzero_pytest_with_full_clone_junit_fails_and_cleans_up(repo: Path) -> None:
    runner = FakePytest(returncode=1, junit=_junit(cases=_cases_except()))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "pytest exited 1" in _details(results)
    assert not (repo / JUNIT_DIR).exists()


def test_junit_dir_argument_is_parsed_with_ignored_default() -> None:
    assert release.parse_args([]).junit_dir == JUNIT_DIR
    custom = release.parse_args(["--junit-dir", ".artifacts/other"])
    assert custom.junit_dir == Path(".artifacts/other")


def test_default_junit_dir_is_ignored_by_the_repository() -> None:
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", str(JUNIT_DIR / "x.xml")],
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    assert completed.returncode == 0


def test_success_runs_manifest_once_and_removes_directory(repo: Path) -> None:
    runner = FakePytest()

    exit_code, results = _gate(repo, runner)

    assert exit_code == 0, _details(results)
    assert len(runner.pytest_calls) == 1
    command = runner.pytest_calls[0]
    assert command[-len(MANIFEST) :] == MANIFEST
    strict = command.index("xfail_strict=true")
    assert command[strict - 1] == "-o"
    assert runner.pytest_timeouts == [release.PYTEST_TIMEOUT_SECONDS]
    assert runner.junit_dir_seen == (repo / JUNIT_DIR).resolve()
    assert not (repo / JUNIT_DIR).exists()
    assert ("git", "ls-files", ".artifacts") in runner.calls


@pytest.mark.parametrize(
    "env",
    [{}, {"POSTGRES_SMOKE_DATABASE_URL": ""}, {"POSTGRES_SMOKE_DATABASE_URL": "  "}],
)
def test_missing_database_url_blocks_before_pytest(
    repo: Path,
    env: Mapping[str, str],
) -> None:
    runner = FakePytest()

    exit_code, results = _gate(repo, runner, env=env)

    assert exit_code == 2
    assert runner.calls == []
    assert not (repo / ".artifacts").exists()
    assert "POSTGRES_SMOKE_DATABASE_URL" in _details(results)
    assert "postgresql://" not in _details(results)


def test_missing_tool_blocks_before_pytest(repo: Path) -> None:
    runner = FakePytest()

    exit_code, _ = _gate(
        repo,
        runner,
        which=lambda name: None if name == "poetry" else _which(name),
    )

    assert exit_code == 2
    assert runner.calls == []


def test_nonzero_pytest_fails_even_with_clean_junit(repo: Path) -> None:
    runner = FakePytest(returncode=1)

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "pytest exited 1" in _details(results)
    assert not (repo / JUNIT_DIR).exists()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('<skipped type="pytest.skip" message="no database">skip</skipped>', "skip"),
        ('<skipped type="pytest.xfail" message="known">xfail</skipped>', "xfail"),
        ('<failure message="boom">trace</failure>', "failures=1"),
        ('<error message="fixture">trace</error>', "errors=1"),
    ],
)
def test_critical_skip_xfail_failure_or_error_fails(
    repo: Path,
    body: str,
    expected: str,
) -> None:
    cases = _cases_except(
        body_for="tests/integration/test_media_assets_postgres.py", body=body
    )
    # Suite attributes deliberately report zero: case bodies alone must fail.
    runner = FakePytest(junit=_junit(cases=cases))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert expected in _details(results)
    assert not (repo / JUNIT_DIR).exists()


def test_suite_attribute_skip_fails_even_without_case_marker(repo: Path) -> None:
    runner = FakePytest(junit=_junit(skipped=1))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "skip" in _details(results)


def test_missing_junit_fails(repo: Path) -> None:
    runner = FakePytest(junit="<absent>")

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "JUnit file is missing" in _details(results)
    assert not (repo / JUNIT_DIR).exists()


@pytest.mark.parametrize(
    "junit",
    [
        "not xml at all",
        "<testsuites><testsuite",
        "<html><body>no junit</body></html>",
        "<testsuites></testsuites>",
        '<testsuite tests="many" failures="0" errors="0" skipped="0"></testsuite>',
        '<testsuite tests="1" failures="-1" errors="0" skipped="0"></testsuite>',
    ],
)
def test_malformed_junit_fails(repo: Path, junit: str) -> None:
    runner = FakePytest(junit=junit)

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "JUnit file is malformed" in _details(results)
    assert not (repo / JUNIT_DIR).exists()


def test_empty_junit_suite_fails(repo: Path) -> None:
    runner = FakePytest(junit=_junit(cases=[]))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "did not record any test case" in _details(results)


def test_manifest_file_without_recorded_cases_fails(repo: Path) -> None:
    cases = _cases_except("tests/integration/test_media_assets_postgres.py")
    runner = FakePytest(junit=_junit(cases=cases))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "tests/integration/test_media_assets_postgres.py" in _details(results)


def test_missing_manifest_file_fails_before_pytest(repo: Path) -> None:
    (repo / MANIFEST[2]).unlink()
    runner = FakePytest()

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert runner.pytest_calls == []
    assert "manifest files are missing" in _details(results)


def test_timeout_fails_and_removes_directory(repo: Path) -> None:
    runner = FakePytest(raises=subprocess.TimeoutExpired(cmd="pytest", timeout=1))

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "timeout" in _details(results)
    assert not (repo / JUNIT_DIR).exists()


def test_unexpected_crash_still_removes_directory(repo: Path) -> None:
    runner = FakePytest(raises=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        _gate(repo, runner)

    assert runner.junit_dir_seen is not None
    assert not (repo / JUNIT_DIR).exists()


def test_system_exit_inside_pytest_still_removes_directory(repo: Path) -> None:
    runner = FakePytest(raises=SystemExit(143))

    with pytest.raises(SystemExit):
        _gate(repo, runner)

    assert runner.junit_dir_seen is not None
    assert not (repo / JUNIT_DIR).exists()


class _SentinelSignal(Exception):
    """Raised by the pre-existing SIGTERM handler if the runner did not swap it."""


def _sentinel_sigterm(signum: int, frame: object) -> None:
    raise _SentinelSignal()


def test_sigterm_during_pytest_exits_143_removes_directory_and_restores_handler(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakePytest(observe=lambda _: os.kill(os.getpid(), signal.SIGTERM))
    monkeypatch.setattr(
        release,
        "execute_gate",
        functools.partial(
            release.execute_gate,
            repository_root=repo,
            manifest=MANIFEST,
            which=_which,
            env=ENV,
            runner=runner,
        ),
    )
    original = signal.signal(signal.SIGTERM, _sentinel_sigterm)
    try:
        with pytest.raises(SystemExit) as exc_info:
            release.main(["--junit-dir", str(JUNIT_DIR)])
        assert signal.getsignal(signal.SIGTERM) is _sentinel_sigterm
    finally:
        signal.signal(signal.SIGTERM, original)

    assert exc_info.value.code == 143
    assert runner.junit_dir_seen == (repo / JUNIT_DIR).resolve()
    assert not (repo / JUNIT_DIR).exists()


def test_usage_error_exits_1_not_blocked(capsys: pytest.CaptureFixture[str]) -> None:
    before = signal.getsignal(signal.SIGTERM)

    with pytest.raises(SystemExit) as exc_info:
        release.main(["--no-such-flag"])

    assert exc_info.value.code == 1
    assert "unrecognized arguments: --no-such-flag" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) is before


def test_help_still_exits_0() -> None:
    with pytest.raises(SystemExit) as exc_info:
        release.parse_args(["--help"])

    assert exc_info.value.code == 0


def test_preexisting_directory_is_refused_and_not_deleted(repo: Path) -> None:
    foreign = repo / JUNIT_DIR / "foreign.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("not ours", encoding="utf-8")
    runner = FakePytest()

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert runner.pytest_calls == []
    assert foreign.read_text(encoding="utf-8") == "not ours"
    assert "already exists" in _details(results)


@pytest.mark.parametrize(
    "junit_dir",
    [
        Path("."),
        Path(".artifacts"),
        Path("reports/clinic-clone-release"),
        Path(".artifacts/../escape"),
        Path("/tmp/clinic-clone-release"),
    ],
)
def test_junit_dir_outside_artifacts_is_rejected_before_anything(
    repo: Path,
    junit_dir: Path,
) -> None:
    runner = FakePytest()

    exit_code, results = _gate(repo, runner, junit_dir=junit_dir)

    assert exit_code == 1
    assert runner.calls == []
    assert "--junit-dir must be a directory inside .artifacts/" in _details(results)


@pytest.mark.parametrize("suffix", ["link", "link/clinic-clone-release"])
def test_junit_dir_symlinked_outside_repository_is_rejected(
    repo: Path,
    tmp_path_factory: pytest.TempPathFactory,
    suffix: str,
) -> None:
    outside = tmp_path_factory.mktemp("outside-repo")
    (outside / "clinic-clone-release").mkdir()
    keep = outside / "clinic-clone-release" / "keep.txt"
    keep.write_text("outside", encoding="utf-8")
    (repo / ".artifacts").mkdir()
    (repo / ".artifacts" / "link").symlink_to(outside, target_is_directory=True)
    runner = FakePytest()

    exit_code, results = _gate(repo, runner, junit_dir=Path(".artifacts") / suffix)

    assert exit_code == 1
    assert runner.calls == []
    assert "--junit-dir must be a directory inside .artifacts/" in _details(results)
    assert (repo / ".artifacts" / "link").is_symlink()
    assert sorted(path.name for path in outside.iterdir()) == ["clinic-clone-release"]
    assert keep.read_text(encoding="utf-8") == "outside"


def test_tracked_artifacts_fail(repo: Path) -> None:
    runner = FakePytest(tracked=".artifacts/clinic-clone-release/leak.xml\n")

    exit_code, results = _gate(repo, runner)

    assert exit_code == 1
    assert "tracked by git" in _details(results)


def test_cli_without_database_url_exits_blocked_without_artifacts() -> None:
    junit_dir = Path(f".artifacts/clinic-clone-release-cli-{uuid4().hex[:8]}")
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "POSTGRES_SMOKE_DATABASE_URL"
    }

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--junit-dir", str(junit_dir)],
        cwd=REPOSITORY_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert completed.returncode == 2, completed.stderr
    assert "POSTGRES_SMOKE_DATABASE_URL" in completed.stderr
    assert "BLOCKED" in completed.stderr
    assert not (REPOSITORY_ROOT / junit_dir).exists()
