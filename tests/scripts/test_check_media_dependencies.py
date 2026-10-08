"""Tests for the fail-closed media dependency assertion."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_media_dependencies.py"
SPEC = importlib.util.spec_from_file_location("check_media_dependencies", SCRIPT)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

SAFE_VERSIONS = {
    "fastapi": "0.142.4",
    "starlette": "1.7.0",
    "python-multipart": "0.0.32",
    "pip-audit": "2.9.0",
}


def write_repository(
    root: Path,
    *,
    fastapi: str = ">=0.142.4,<0.143.0",
    multipart: str = ">=0.0.32,<0.1.0",
    audit: str = "==2.9.0",
    direct_starlette: bool = False,
    poetry_main_extra: str = "",
    poetry_dev_extra: str = "",
    requirements_extra: str = "",
    development_extra: str = "",
    locked: dict[str, str] | None = None,
) -> None:
    starlette_line = '\nstarlette = ">=1.0.1"' if direct_starlette else ""
    (root / "pyproject.toml").write_text(
        f"""[tool.poetry.dependencies]
python = "^3.11"
fastapi = "{fastapi}"
python-multipart = "{multipart}"{starlette_line}
{poetry_main_extra}

[tool.poetry.group.dev.dependencies]
pip-audit = "{audit}"
{poetry_dev_extra}
""",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text(
        f"fastapi{fastapi}\npython-multipart{multipart}\n{requirements_extra}",
        encoding="utf-8",
    )
    (root / "requirements-dev.txt").write_text(
        f"-r requirements.txt\npip-audit{audit}\n{development_extra}",
        encoding="utf-8",
    )
    lock_versions = SAFE_VERSIONS if locked is None else locked
    packages = "\n".join(
        f'[[package]]\nname = "{name}"\nversion = "{version}"'
        for name, version in lock_versions.items()
    )
    (root / "poetry.lock").write_text(packages, encoding="utf-8")


def test_accepts_matching_safe_manifests_lock_and_environment(tmp_path: Path) -> None:
    write_repository(tmp_path)

    resolved, errors = checker.check_dependencies(tmp_path, SAFE_VERSIONS)

    assert errors == []
    assert resolved == SAFE_VERSIONS


@pytest.mark.parametrize(
    ("field", "unsafe"),
    [("fastapi", ">=0.142.3,<0.143.0"), ("multipart", ">=0.0.31,<0.1.0")],
)
def test_rejects_unapproved_manifest_ranges(
    tmp_path: Path, field: str, unsafe: str
) -> None:
    write_repository(tmp_path, **{field: unsafe})

    _, errors = checker.check_dependencies(tmp_path, SAFE_VERSIONS)

    assert any("!= approved" in error for error in errors)


def test_rejects_a_direct_starlette_pin(tmp_path: Path) -> None:
    write_repository(tmp_path, direct_starlette=True)

    _, errors = checker.check_dependencies(tmp_path, SAFE_VERSIONS)

    assert "pyproject.toml must not declare Starlette directly" in errors


@pytest.mark.parametrize("missing", SAFE_VERSIONS)
def test_rejects_packages_missing_from_resolved_environment(
    tmp_path: Path, missing: str
) -> None:
    write_repository(tmp_path)
    installed = dict(SAFE_VERSIONS)
    installed.pop(missing)

    _, errors = checker.check_dependencies(tmp_path, installed)

    assert f"resolved environment is missing {missing}" in errors


@pytest.mark.parametrize("package", SAFE_VERSIONS)
def test_rejects_resolved_packages_that_diverge_from_lock(
    tmp_path: Path, package: str
) -> None:
    write_repository(tmp_path)
    installed = dict(SAFE_VERSIONS)
    installed[package] = "999.0.0"

    _, errors = checker.check_dependencies(tmp_path, installed)

    assert any(f"resolved {package} 999.0.0 diverges" in error for error in errors)


def test_rejects_package_missing_from_lock(tmp_path: Path) -> None:
    locked = dict(SAFE_VERSIONS)
    locked.pop("python-multipart")
    write_repository(tmp_path, locked=locked)

    _, errors = checker.check_dependencies(tmp_path, SAFE_VERSIONS)

    assert "poetry.lock is missing python-multipart" in errors


def test_rejects_starlette_below_minimum(tmp_path: Path) -> None:
    unsafe = {**SAFE_VERSIONS, "starlette": "1.0.0"}
    write_repository(tmp_path, locked=unsafe)

    _, errors = checker.check_dependencies(tmp_path, unsafe)

    assert "resolved starlette 1.0.0 is below 1.0.1" in errors


def test_rejects_unpinned_audit_tool(tmp_path: Path) -> None:
    write_repository(tmp_path, audit=">=2.9.0")

    _, errors = checker.check_dependencies(tmp_path, SAFE_VERSIONS)

    assert "pyproject.toml must pin pip-audit to one exact version" in errors


@pytest.mark.parametrize(
    ("main", "dev"),
    [
        ('fastapi = ">=0.142.3,<0.143.0"', 'fastapi = ">=0.142.4,<0.143.0"'),
        ('fastapi = ">=0.142.4,<0.143.0"', 'fastapi = ">=0.142.3,<0.143.0"'),
    ],
)
def test_rejects_poetry_group_shadowing(tmp_path: Path, main: str, dev: str) -> None:
    # Replace the fixture's normal main declaration so TOML remains valid while
    # the same package is declared in two distinct Poetry dependency groups.
    write_repository(
        tmp_path,
        fastapi=main.split('"')[1],
        poetry_dev_extra=dev,
    )

    _, errors = checker.check_dependencies(tmp_path, SAFE_VERSIONS)

    assert (
        "pyproject.toml declares fastapi more than once across dependency groups"
        in errors
    )


@pytest.mark.parametrize(
    ("primary", "duplicate"),
    [
        (">=0.142.3,<0.143.0", ">=0.142.4,<0.143.0"),
        (">=0.142.4,<0.143.0", ">=0.142.3,<0.143.0"),
    ],
)
def test_rejects_requirements_shadowing(
    tmp_path: Path, primary: str, duplicate: str
) -> None:
    write_repository(
        tmp_path,
        fastapi=primary,
        development_extra=f"fastapi{duplicate}\n",
    )

    _, errors = checker.check_dependencies(tmp_path, SAFE_VERSIONS)

    assert "requirements manifests declare fastapi more than once" in errors
