from __future__ import annotations

import os
import subprocess
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "packages" / "neurostack-core"
SCRIPT = ROOT / "scripts" / "assert-neurostack-core-origin.py"


@pytest.fixture(scope="module")
def installs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("origin-installs")
    result: dict[str, Path] = {}
    for mode in ("editable", "noneditable"):
        environment = root / mode
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin" / "python"
        command = [str(python), "-m", "pip", "install"]
        if mode == "editable":
            command.append("--editable")
        command.append(str(PACKAGE))
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        result[mode] = python
    result["cwd"] = root
    return result


def run_oracle(
    python: Path,
    cwd: Path,
    mode: str,
    expected: Path | None = None,
    pythonpath: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(python), str(SCRIPT), "--mode", mode]
    if expected is not None:
        command.extend(["--expected-source", str(expected)])
    env = os.environ.copy()
    env["PYTHONTZPATH"] = ""
    if pythonpath is None:
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, check=False
    )


def test_accepts_editable_origin_with_direct_url_and_tzdata(
    installs: dict[str, Path],
) -> None:
    result = run_oracle(installs["editable"], installs["cwd"], "editable", PACKAGE)
    assert result.returncode == 0, result.stdout + result.stderr


def test_accepts_noneditable_origin_inside_environment(
    installs: dict[str, Path],
) -> None:
    result = run_oracle(
        installs["noneditable"], installs["cwd"], "noneditable", PACKAGE
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_wrong_source_and_pythonpath_leakage(
    installs: dict[str, Path], tmp_path: Path
) -> None:
    wrong = run_oracle(
        installs["editable"], installs["cwd"], "editable", tmp_path / "wrong"
    )
    assert wrong.returncode == 1
    leaked = run_oracle(
        installs["editable"],
        installs["cwd"],
        "editable",
        PACKAGE,
        pythonpath=str(PACKAGE),
    )
    assert leaked.returncode == 1
    assert "PYTHONPATH" in leaked.stderr


def test_rejects_missing_distribution(tmp_path: Path) -> None:
    environment = tmp_path / "missing"
    venv.EnvBuilder(with_pip=True).create(environment)
    shadow = tmp_path / "shadow"
    package = shadow / "neurostack_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    result = run_oracle(
        environment / "bin" / "python",
        tmp_path,
        "editable",
        shadow,
        pythonpath=str(shadow),
    )
    assert result.returncode == 1
    assert "package metadata" in result.stderr.lower()


def test_rejects_absent_editable_metadata(
    installs: dict[str, Path], tmp_path: Path
) -> None:
    python = installs["editable"]
    locate = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; print(m.distribution('neurostack-core')._path)",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    direct_url = Path(locate.stdout.strip()) / "direct_url.json"
    saved = direct_url.read_bytes()
    direct_url.unlink()
    try:
        result = run_oracle(python, tmp_path, "editable", PACKAGE)
    finally:
        direct_url.write_bytes(saved)
    assert result.returncode == 1
    assert "direct_url" in result.stderr


def test_rejects_cwd_shadow(installs: dict[str, Path], tmp_path: Path) -> None:
    shadow = tmp_path / "neurostack_core"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")
    result = run_oracle(installs["noneditable"], tmp_path, "noneditable", PACKAGE)
    assert result.returncode == 1
    assert "shadow" in result.stderr


@pytest.mark.parametrize("requirement", ["pydantic", "prometheus-client", "tzdata"])
def test_rejects_missing_core_dependency_provenance(
    installs: dict[str, Path], requirement: str
) -> None:
    python = installs["noneditable"]
    locate = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.metadata as m; print(m.distribution('neurostack-core')._path)",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    metadata_file = Path(locate.stdout.strip()) / "METADATA"
    saved = metadata_file.read_text()
    normalized = requirement.replace("-", "_")
    filtered = (
        "\n".join(
            line
            for line in saved.splitlines()
            if not (
                line.lower().startswith("requires-dist:")
                and line.lower()
                .split(":", 1)[1]
                .strip()
                .replace("-", "_")
                .startswith(normalized)
            )
        )
        + "\n"
    )
    metadata_file.write_text(filtered)
    try:
        result = run_oracle(python, installs["cwd"], "noneditable", PACKAGE)
    finally:
        metadata_file.write_text(saved)
    assert result.returncode == 1
    assert "missing requirements" in result.stderr
    assert requirement in result.stderr
