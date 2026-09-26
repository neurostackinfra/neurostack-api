from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "capture-core-branch-baseline.py"


def make_minimal_tree(path: Path) -> None:
    for target in ("srm", "flow", "policy", "prompt"):
        directory = path / "tests" / target
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "test_placeholder.py").write_text("def test_ok(): assert True\n")


def make_python_shim(path: Path, mode: str = "valid") -> None:
    source = f"""#!{sys.executable}
import sys
print('============================= test session starts ==============================')
if {mode!r} == 'exit2':
    print('collected 3 items')
    print('============================== 3 passed in 0.01s ==============================')
    raise SystemExit(2)
if {mode!r} == 'partial':
    print('collected 3 items')
    raise SystemExit(0)
focused = sys.argv[-1] != 'tests'
if focused:
    print('collected 5 items')
    print('============================== 5 passed in 0.01s ==============================')
    raise SystemExit(0)
print('collected 3 items')
print('FAILED tests/test_placeholder.py::test_bad - AssertionError')
print('========================= 1 failed, 2 passed in 0.01s =========================')
raise SystemExit(1)
"""
    path.write_text(source)
    path.chmod(path.stat().st_mode | 0o111)


def run_capture(
    tree: Path, shim: Path, output: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--tree",
            str(tree),
            "--base-sha",
            "a" * 40,
            "--output",
            str(output),
            "--python",
            str(shim),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_two_archives_generate_byte_identical_baselines(tmp_path: Path) -> None:
    repo = tmp_path / "tree"
    repo.mkdir()
    make_minimal_tree(repo)
    shim = tmp_path / "python-shim"
    make_python_shim(shim)
    first, second = tmp_path / "first", tmp_path / "second"
    assert run_capture(repo, shim, first).returncode == 0
    assert run_capture(repo, shim, second).returncode == 0
    assert {path.name: path.read_bytes() for path in first.iterdir()} == {
        path.name: path.read_bytes() for path in second.iterdir()
    }
    assert sorted(path.name for path in first.iterdir()) == [
        "neurostack-core-extraction-branch.txt",
        "neurostack-core-extraction-known-failures.txt",
        "neurostack-core-extraction-outcome-floor.txt",
        "neurostack-core-extraction-pass-floor.txt",
    ]


def make_multi_failure_shim(path: Path) -> None:
    source = f"""#!{sys.executable}
import sys
print('============================= test session starts ==============================')
focused = sys.argv[-1] != 'tests'
if focused:
    print('collected 5 items')
    print('============================== 5 passed in 0.01s ==============================')
    raise SystemExit(0)
print('collected 4 items')
print('FAILED tests/test_placeholder.py::test_alpha - AssertionError')
print('FAILED tests/test_placeholder.py::test_beta - AssertionError')
print('========================= 2 failed, 2 passed in 0.01s =========================')
raise SystemExit(1)
"""
    path.write_text(source)
    path.chmod(path.stat().st_mode | 0o111)


def test_multiple_failed_lines_are_all_recorded(tmp_path: Path) -> None:
    repo = tmp_path / "tree"
    repo.mkdir()
    make_minimal_tree(repo)
    shim = tmp_path / "python-shim"
    make_multi_failure_shim(shim)
    output = tmp_path / "output"
    result = run_capture(repo, shim, output)
    assert result.returncode == 0, result.stderr
    failures = (
        (output / "neurostack-core-extraction-known-failures.txt")
        .read_text()
        .splitlines()
    )
    assert failures == [
        "FAILED tests/test_placeholder.py::test_alpha",
        "FAILED tests/test_placeholder.py::test_beta",
    ]


@pytest.mark.parametrize("mode", ["partial", "exit2"])
def test_rejects_partial_summary_and_non_0_1_exit(tmp_path: Path, mode: str) -> None:
    repo = tmp_path / "tree"
    repo.mkdir()
    make_minimal_tree(repo)
    shim = tmp_path / "python-shim"
    make_python_shim(shim, mode)
    result = run_capture(repo, shim, tmp_path / "output")
    assert result.returncode == 2
    assert not (tmp_path / "output").exists()
