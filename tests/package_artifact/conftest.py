"""Shared fresh artifacts and external install for package consumer tests."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "packages" / "neurostack-core"


@dataclass(frozen=True)
class ArtifactBuild:
    """The only wheel and sdist built by this pytest session."""

    workspace: Path
    wheel: Path
    sdist: Path
    output: str


@dataclass(frozen=True)
class ExternalInstall:
    """A wheel install whose interpreter and cwd are outside the checkout."""

    workspace: Path
    python: Path
    site_packages: Path
    dist_info: Path


def clean_environment(*, timezone: bool = False) -> dict[str, str]:
    """Return the minimal deterministic environment for consumer execution."""
    environment = {"LC_ALL": "C.UTF-8"}
    if timezone:
        environment["PYTHONTZPATH"] = ""
    return environment


def external_environment() -> dict[str, str]:
    """Return the host environment without import-path leakage."""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return environment


def isolated_pip_environment(workspace: Path) -> dict[str, str]:
    """Return a credential-free pip/build environment rooted in workspace."""
    directories = {
        "HOME": workspace / "home",
        "TMPDIR": workspace / "tmp",
        "XDG_CACHE_HOME": workspace / "xdg-cache",
        "PIP_CACHE_DIR": workspace / "pip-cache",
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_INDEX_URL": "https://pypi.org/simple",
        "PIP_NO_INPUT": "1",
        **{name: str(path) for name, path in directories.items()},
    }


@pytest.fixture(scope="session")
def artifact_build(tmp_path_factory: pytest.TempPathFactory) -> ArtifactBuild:
    """Build exactly one fresh wheel and one fresh sdist."""
    workspace = tmp_path_factory.mktemp("neurostack-core-artifacts")
    output_dir = workspace / "dist"
    output_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--outdir",
            str(output_dir),
            str(PACKAGE),
        ],
        cwd=workspace,
        env=isolated_pip_environment(workspace),
        text=True,
        capture_output=True,
        check=False,
    )
    transcript = result.stdout + result.stderr
    assert result.returncode == 0, transcript
    assert re.search(r"warning.*(?:missing|not found)", transcript, re.I) is None

    wheels = list(output_dir.glob("*.whl"))
    sdists = list(output_dir.glob("*.tar.gz"))
    assert len(wheels) == 1, wheels
    assert len(sdists) == 1, sdists
    assert set(output_dir.iterdir()) == {wheels[0], sdists[0]}
    return ArtifactBuild(workspace, wheels[0], sdists[0], transcript)


@pytest.fixture
def external_install(
    artifact_build: ArtifactBuild,
    tmp_path_factory: pytest.TempPathFactory,
) -> ExternalInstall:
    """Install the fresh wheel and its dependency closure outside the repo."""
    workspace = tmp_path_factory.mktemp("neurostack-core-consumer")
    assert not workspace.is_relative_to(ROOT)
    wheel = workspace / artifact_build.wheel.name
    shutil.copy2(artifact_build.wheel, wheel)

    environment = isolated_pip_environment(workspace)
    environment["PIP_REQUIRE_VIRTUALENV"] = "1"
    venv_dir = workspace / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    executable = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run(
        [str(executable), "-m", "pip", "install", str(wheel)],
        cwd=workspace,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    probe = subprocess.run(
        [
            str(executable),
            "-c",
            (
                "import importlib.metadata, json, pathlib; "
                "d=importlib.metadata.distribution('neurostack-core'); "
                "print(json.dumps([str(pathlib.Path(d._path).parent), "
                "str(pathlib.Path(d._path))]))"
            ),
        ],
        cwd=workspace,
        env=clean_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    site_packages_raw, dist_info_raw = json.loads(probe.stdout)
    return ExternalInstall(
        workspace,
        executable,
        Path(site_packages_raw).resolve(),
        Path(dist_info_raw).resolve(),
    )
