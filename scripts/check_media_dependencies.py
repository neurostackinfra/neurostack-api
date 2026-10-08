#!/usr/bin/env python3
"""Fail closed unless the multipart dependency stack is safe and reproducible."""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPROVED_RANGES = {
    "fastapi": SpecifierSet(">=0.142.4,<0.143.0"),
    "python-multipart": SpecifierSet(">=0.0.32,<0.1.0"),
}
MINIMUM_STARLETTE = Version("1.0.1")
AUDIT_PACKAGE = "pip-audit"


def _specifier(value: Any) -> SpecifierSet:
    if isinstance(value, dict):
        value = value.get("version")
    if not isinstance(value, str):
        raise InvalidSpecifier(f"expected a version string, got {value!r}")
    return SpecifierSet(value)


def _requirements(path: Path) -> dict[str, list[Requirement]]:
    requirements: dict[str, list[Requirement]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-r ", "--requirement ", "-e ")):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            continue
        requirements.setdefault(canonicalize_name(requirement.name), []).append(
            requirement
        )
    return requirements


def _poetry_dependencies(document: Mapping[str, Any]) -> dict[str, list[Any]]:
    poetry = document.get("tool", {}).get("poetry", {})
    dependencies: dict[str, list[Any]] = {}
    sections = [poetry.get("dependencies", {})]
    sections.extend(
        group.get("dependencies", {}) for group in poetry.get("group", {}).values()
    )
    for section in sections:
        for name, value in section.items():
            dependencies.setdefault(canonicalize_name(name), []).append(value)
    return dependencies


def _only(values: list[Any] | None) -> Any:
    if not values:
        return None
    return values[0]


def _locked_versions(
    document: Mapping[str, Any], checked_names: set[str]
) -> tuple[dict[str, str], list[str]]:
    versions: dict[str, str] = {}
    errors: list[str] = []
    for package in document.get("package", []):
        name = canonicalize_name(package["name"])
        if name not in checked_names:
            continue
        version = package["version"]
        previous = versions.get(name)
        if previous is not None and previous != version:
            errors.append(
                f"poetry.lock resolves multiple {name} versions: {previous}, {version}"
            )
        versions[name] = version
    return versions, errors


def _installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in (*APPROVED_RANGES, "starlette", AUDIT_PACKAGE):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return versions


def check_dependencies(
    repository_root: Path = REPOSITORY_ROOT,
    installed_versions: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Return resolved versions and every violation found in manifests/runtime."""

    errors: list[str] = []
    try:
        pyproject = tomllib.loads(
            (repository_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        lock = tomllib.loads(
            (repository_root / "poetry.lock").read_text(encoding="utf-8")
        )
        requirements = _requirements(repository_root / "requirements.txt")
        development = _requirements(repository_root / "requirements-dev.txt")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, [f"cannot read dependency manifests: {exc}"]

    poetry_dependencies = _poetry_dependencies(pyproject)
    if "starlette" in poetry_dependencies:
        errors.append("pyproject.toml must not declare Starlette directly")
    if "starlette" in requirements or "starlette" in development:
        errors.append("requirements manifests must not declare Starlette directly")

    relevant_names = {*APPROVED_RANGES, "starlette", AUDIT_PACKAGE}
    for name in sorted(relevant_names):
        poetry_declarations = poetry_dependencies.get(name, [])
        requirement_declarations = [
            *requirements.get(name, []),
            *development.get(name, []),
        ]
        if len(poetry_declarations) > 1:
            errors.append(
                f"pyproject.toml declares {name} more than once across dependency groups"
            )
        if len(requirement_declarations) > 1:
            errors.append(f"requirements manifests declare {name} more than once")

    for name, approved in APPROVED_RANGES.items():
        try:
            poetry_range = _specifier(_only(poetry_dependencies.get(name)))
        except InvalidSpecifier as exc:
            errors.append(f"pyproject.toml has invalid {name} range: {exc}")
        else:
            if poetry_range != approved:
                errors.append(
                    f"pyproject.toml {name} range {poetry_range} != approved {approved}"
                )

        requirement = _only(requirements.get(name))
        if requirement is None:
            errors.append(f"requirements.txt is missing {name}")
        elif requirement.specifier != approved:
            errors.append(
                f"requirements.txt {name} range {requirement.specifier} "
                f"!= approved {approved}"
            )

    audit_poetry_value = _only(poetry_dependencies.get(AUDIT_PACKAGE))
    audit_requirement = _only(development.get(AUDIT_PACKAGE))
    try:
        audit_poetry_range = _specifier(audit_poetry_value)
    except InvalidSpecifier as exc:
        errors.append(f"pyproject.toml must pin pip-audit exactly: {exc}")
        audit_poetry_range = SpecifierSet()
    audit_dev_range = (
        audit_requirement.specifier if audit_requirement is not None else SpecifierSet()
    )
    audit_pins = {
        spec.version
        for spec in audit_poetry_range
        if spec.operator in {"==", "==="} and "*" not in spec.version
    }
    if len(audit_poetry_range) != 1 or len(audit_pins) != 1:
        errors.append("pyproject.toml must pin pip-audit to one exact version")
    if audit_requirement is None:
        errors.append("requirements-dev.txt is missing pip-audit")
    elif audit_dev_range != audit_poetry_range:
        errors.append("pip-audit pins differ between development manifests")

    checked_names = {*APPROVED_RANGES, "starlette", AUDIT_PACKAGE}
    locked, lock_errors = _locked_versions(lock, checked_names)
    errors.extend(lock_errors)
    installed = {
        canonicalize_name(name): version
        for name, version in (
            installed_versions
            if installed_versions is not None
            else _installed_versions()
        ).items()
    }
    checked_names = (*APPROVED_RANGES, "starlette", AUDIT_PACKAGE)
    resolved: dict[str, str] = {}
    for name in checked_names:
        locked_version = locked.get(name)
        installed_version = installed.get(name)
        if locked_version is None:
            errors.append(f"poetry.lock is missing {name}")
            continue
        if installed_version is None:
            errors.append(f"resolved environment is missing {name}")
            continue
        resolved[name] = installed_version
        if installed_version != locked_version:
            errors.append(
                f"resolved {name} {installed_version} diverges from lock {locked_version}"
            )
            continue
        try:
            version = Version(installed_version)
        except InvalidVersion:
            errors.append(f"resolved {name} has invalid version {installed_version!r}")
            continue
        if name in APPROVED_RANGES and version not in APPROVED_RANGES[name]:
            errors.append(
                f"resolved {name} {version} is outside {APPROVED_RANGES[name]}"
            )
        if name == "starlette" and version < MINIMUM_STARLETTE:
            errors.append(f"resolved starlette {version} is below {MINIMUM_STARLETTE}")
        if name == AUDIT_PACKAGE and audit_pins and str(version) not in audit_pins:
            errors.append(
                f"resolved pip-audit {version} differs from manifest pin "
                f"{next(iter(audit_pins))}"
            )

    return resolved, errors


def main() -> int:
    resolved, errors = check_dependencies()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    versions = " ".join(f"{name}={resolved[name]}" for name in sorted(resolved))
    print(f"media dependency check passed: {versions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
