"""Contract of the no-skip credit integration gate.

``scripts/check-credit-integration.sh`` runs ``poetry run pytest -q -rs -p
no:cacheprovider <paths>`` and accepts only a run in which every collected
test passed.  Most proofs here run REAL pytest on throwaway test modules: the
gate is copied into a scratch project (its root is the script's parent), and
a ``poetry`` shim on ``PATH`` runs this interpreter's pytest.  A few outcome
shapes pytest cannot be made to print on demand use a canned transcript.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-credit-integration.sh"

_PASSING = """
def test_one():
    assert True


def test_two():
    assert 1 + 1 == 2
"""
_MODULES = {
    "skip": """
import pytest


def test_ok():
    pass


def test_needs_a_service():
    pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
""",
    "module_skip": """
import pytest

pytest.skip("service env var missing", allow_module_level=True)


def test_never_runs():
    pass
""",
    "fixture_skip": """
import pytest


@pytest.fixture(scope="module")
def harness():
    pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")


def test_uses_the_harness(harness):
    pass


def test_ok():
    pass
""",
    "xfail": """
import pytest


def test_ok():
    pass


@pytest.mark.xfail(reason="known")
def test_known_failure():
    assert False
""",
    "xpass": """
import pytest


def test_ok():
    pass


@pytest.mark.xfail(reason="flaky", strict=False)
def test_unexpectedly_passes():
    assert True
""",
    "failure": """
def test_ok():
    pass


def test_broken():
    assert 1 == 2
""",
    "error": """
import pytest


@pytest.fixture
def broken():
    raise RuntimeError("setup failed")


def test_ok():
    pass


def test_errors(broken):
    pass
""",
    "empty": """
# No test functions: an empty selection.
VALUE = 1
""",
    "exit_zero": """
import pytest


def test_first():
    pass


def test_stops_the_session():
    pytest.exit("stopping early", returncode=0)


def test_never_runs():
    pass
""",
}


def _project(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A scratch project holding a copy of the gate and a poetry shim."""

    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "tests").mkdir()
    shutil.copy2(SCRIPT, project / "scripts" / SCRIPT.name)
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    args_file = tmp_path / "args.txt"
    shim = shim_dir / "poetry"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = run ] && shift\n'
        '[ "$1" = pytest ] && shift\n'
        'case " $* " in\n'
        '    *" --collect-only "*)\n'
        '        printf "%s\\n" "$*" >> "$COLLECT_FILE"\n'
        '        if [ -n "${CANNED_TRANSCRIPT:-}" ]; then\n'
        '            echo "tests/test_any.py::test_any"\n'
        '            echo "${CANNED_COLLECTED:-4} tests collected in 0.01s"\n'
        "            exit 0\n"
        "        fi\n"
        '        exec "$TEST_PYTHON" -m pytest "$@" ;;\n'
        "esac\n"
        'printf "%s\\n" "$*" > "$ARGS_FILE"\n'
        'if [ -n "${CANNED_TRANSCRIPT:-}" ]; then\n'
        '    cat "$CANNED_TRANSCRIPT"\n'
        '    exit "${CANNED_STATUS:-0}"\n'
        "fi\n"
        'exec "$TEST_PYTHON" -m pytest "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"PYTHONPATH", "PYTEST_ADDOPTS", "CANNED_TRANSCRIPT", "CANNED_COLLECTED"}
    }
    environment.update(
        {
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "ARGS_FILE": str(args_file),
            "COLLECT_FILE": str(tmp_path / "collect.txt"),
            "TEST_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return project, args_file, environment


def _gate(
    project: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(project / "scripts" / SCRIPT.name), *arguments],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _module(project: Path, name: str, source: str) -> str:
    path = project / "tests" / f"test_{name}.py"
    path.write_text(source, encoding="utf-8")
    return f"tests/test_{name}.py"


def test_a_fully_passing_run_is_accepted(tmp_path: Path) -> None:
    project, args_file, environment = _project(tmp_path)
    first = _module(project, "first", _PASSING)
    second = _module(project, "second", _PASSING)

    result = _gate(project, environment, first, second)

    assert result.returncode == 0, result.stdout + result.stderr
    assert args_file.read_text(encoding="utf-8").strip() == (
        f"-q -rs -p no:cacheprovider {first} {second}"
    )
    # Each path was first collected on its own.
    assert (tmp_path / "collect.txt").read_text(encoding="utf-8").splitlines() == [
        f"--collect-only -q -p no:cacheprovider {first}",
        f"--collect-only -q -p no:cacheprovider {second}",
    ]
    assert "credit integration evidence valid (4 passed of 4 collected" in result.stdout


def test_a_run_cut_short_with_exit_code_zero_is_refused(tmp_path: Path) -> None:
    """``pytest.exit(..., returncode=0)`` mid-module: pytest exits 0 and
    prints ``1 passed``; the module collects 3, so the gate refuses."""

    project, _, environment = _project(tmp_path)
    cut = _module(project, "exit_zero", _MODULES["exit_zero"])

    result = _gate(project, environment, cut)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "passed 1 of the 3 tests its paths collect" in result.stderr
    assert "evidence valid" not in result.stdout


def test_an_empty_module_beside_passing_ones_is_refused(tmp_path: Path) -> None:
    """pytest itself prints ``2 passed`` here; the per-path check refuses."""

    project, args_file, environment = _project(tmp_path)
    passing = _module(project, "passing", _PASSING)
    empty = _module(project, "empty", _MODULES["empty"])

    result = _gate(project, environment, passing, empty)

    assert result.returncode == 1
    assert f"'{empty}' selects no test" in result.stderr
    assert not args_file.exists()


@pytest.mark.parametrize("case", sorted(_MODULES))
def test_any_outcome_but_pass_is_refused(tmp_path: Path, case: str) -> None:
    project, _, environment = _project(tmp_path)
    passing = _module(project, "passing", _PASSING)
    probe = _module(project, case, _MODULES[case])

    result = _gate(project, environment, passing, probe)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "evidence valid" not in result.stdout


def test_an_empty_selection_alone_is_refused(tmp_path: Path) -> None:
    project, _, environment = _project(tmp_path)
    empty = _module(project, "empty", _MODULES["empty"])

    result = _gate(project, environment, empty)

    assert result.returncode == 1
    assert "selects no test (collection exited 5)" in result.stderr


def test_a_skip_alone_fails_even_though_pytest_exits_zero(tmp_path: Path) -> None:
    project, _, environment = _project(tmp_path)
    skipped = _module(project, "skip", _MODULES["skip"])

    result = _gate(project, environment, skipped)

    assert result.returncode == 1
    assert "forbidden pytest outcome" in result.stderr
    assert "1 skipped" in result.stdout


def test_a_deselected_test_is_refused(tmp_path: Path) -> None:
    """A conftest that deselects (pytest exits 0 with N passed)."""

    project, _, environment = _project(tmp_path)
    (project / "tests" / "conftest.py").write_text(
        "def pytest_collection_modifyitems(config, items):\n"
        "    dropped = [item for item in items if item.name == 'test_two']\n"
        "    if dropped:\n"
        "        config.hook.pytest_deselected(items=dropped)\n"
        "        items[:] = [item for item in items if item not in dropped]\n",
        encoding="utf-8",
    )
    target = _module(project, "deselect", _PASSING)

    result = _gate(project, environment, target)

    assert result.returncode == 1
    assert "1 deselected" in result.stdout
    assert "forbidden pytest outcome" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("-k", "x"),
        ("",),
        ("--runxfail",),
        ("tests/test_passing.py", "tests/test_passing.py"),
    ],
)
def test_usage_is_paths_only(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    project, args_file, environment = _project(tmp_path)
    _module(project, "passing", _PASSING)

    result = _gate(project, environment, *arguments)

    assert result.returncode == 2
    assert not args_file.exists()


@pytest.mark.parametrize(
    ("transcript", "status", "collected", "accepted"),
    [
        ("28 passed, 3 warnings in 236.95s (0:03:56)\n", 0, 28, True),
        ("=== 4 passed in 0.42s ===\n", 0, 4, True),
        ("=== 1 passed, 1 warning in 0.42s ===\n", 0, 1, True),
        ("=== 4 passed in 0.42s ===\n", 0, 5, False),
        ("=== 4 passed in 0.42s ===\n", 0, 3, False),
        ("4 passed, 1 deselected in 0.42s\n", 0, 4, False),
        ("4 passed, 1 xfailed in 0.42s\n", 0, 4, False),
        ("4 passed, 2 errors in 0.42s\n", 0, 4, False),
        ("....\n", 0, 4, False),
        ("4 passed in 0.42s\n4 passed in 0.40s\n", 0, 4, False),
        ("4 passed in 0.42s\n", 3, 4, False),
    ],
)
def test_transcript_rules(
    tmp_path: Path, transcript: str, status: int, collected: int, accepted: bool
) -> None:
    project, _, environment = _project(tmp_path)
    canned = tmp_path / "transcript.txt"
    canned.write_text(transcript, encoding="utf-8")
    environment.update(
        {
            "CANNED_TRANSCRIPT": str(canned),
            "CANNED_STATUS": str(status),
            "CANNED_COLLECTED": str(collected),
        }
    )

    result = _gate(project, environment, "tests/test_any.py")

    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


def test_the_gate_is_tracked_executable_and_invoked_with_bash() -> None:
    staged = subprocess.run(
        ["git", "ls-files", "-s", "scripts/check-credit-integration.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert staged and staged[0] == "100755"
    first_line = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == "#!/usr/bin/env bash"
