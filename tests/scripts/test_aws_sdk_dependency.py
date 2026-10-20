"""boto3 and botocore are locked main dependencies.

The KMS receipt and grant signers (``src/api/routes/billing_producer.py`` and
``src/api/routes/billing_producer_setup.py``) import boto3 at request time and
fail closed with "AWS SDK for Python is unavailable" when it is missing. The
production image installs only the main group (``poetry install --only main``),
so a lock that carries the SDK only in another group, as an optional package
or as an extra of pydantic-settings ships an image whose signers always refuse.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "poetry.lock"
REQUIREMENTS = ROOT / "requirements.txt"
AWS_SDK = ("boto3", "botocore")
# The production image: CPython 3.11 on linux/amd64.
IMAGE_ENVIRONMENT = {
    "implementation_name": "cpython",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_system": "Linux",
    "python_full_version": "3.11.17",
    "python_version": "3.11",
    "sys_platform": "linux",
    "extra": "",
}

Document = dict[str, Any]


def _toml(path: Path) -> Document:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _applies(marker: str | None) -> bool:
    return marker is None or Marker(marker).evaluate(IMAGE_ENVIRONMENT)


def _main_marker(package: Document) -> str | None:
    markers = package.get("markers")
    return markers.get("main") if isinstance(markers, dict) else markers


def _dependency_markers(value: Any) -> list[str | None]:
    """Markers of the non-optional constraints of one locked dependency."""

    constraints = value if isinstance(value, list) else [value]
    return [
        constraint.get("markers") if isinstance(constraint, dict) else None
        for constraint in constraints
        if not (isinstance(constraint, dict) and constraint.get("optional"))
    ]


def aws_sdk_lock_problems(pyproject: Document, lock: Document) -> list[str]:
    """Why the production image would not install the AWS SDK, if it would not."""

    problems: list[str] = []
    poetry = pyproject["tool"]["poetry"]
    main = {canonicalize_name(name) for name in poetry["dependencies"]}
    for group, data in poetry.get("group", {}).items():
        for name in data.get("dependencies", {}):
            if canonicalize_name(name) in AWS_SDK:
                problems.append(f"pyproject.toml declares {name} in group {group}")
    packages: dict[str, list[Document]] = {}
    for package in lock.get("package", []):
        packages.setdefault(canonicalize_name(package["name"]), []).append(package)

    pending, seen = list(AWS_SDK), set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in AWS_SDK and name not in main:
            problems.append(f"{name} is not a main dependency in pyproject.toml")
        entries = packages.get(name, [])
        if len(entries) != 1:
            problems.append(f"poetry.lock has {len(entries)} entries for {name}")
            continue
        package = entries[0]
        if "main" not in package.get("groups", []):
            problems.append(f"poetry.lock {name} is not in the main group")
        elif package.get("optional", False):
            problems.append(f"poetry.lock {name} is optional")
        elif not _applies(_main_marker(package)):
            problems.append(f"poetry.lock {name} is not installed on linux/py3.11")
        for dependency, value in package.get("dependencies", {}).items():
            if any(_applies(marker) for marker in _dependency_markers(value)):
                pending.append(canonicalize_name(dependency))
    return problems


def test_aws_sdk_and_its_closure_are_main_group_packages() -> None:
    assert aws_sdk_lock_problems(_toml(PYPROJECT), _toml(LOCK)) == []


def _without(lock: Document, name: str) -> Document:
    lock["package"] = [p for p in lock["package"] if p["name"] != name]
    return lock


def _patched(lock: Document, name: str, **changes: Any) -> Document:
    for package in lock["package"]:
        if package["name"] == name:
            package.update(changes)
    return lock


def _move_to_dev(pyproject: Document, name: str) -> Document:
    poetry = pyproject["tool"]["poetry"]
    poetry["group"]["dev"]["dependencies"][name] = poetry["dependencies"].pop(name)
    return pyproject


@pytest.mark.parametrize(
    ("mutate_pyproject", "mutate_lock", "message"),
    [
        (None, lambda lock: _without(lock, "boto3"), "0 entries for boto3"),
        (None, lambda lock: _without(lock, "botocore"), "0 entries for botocore"),
        (
            None,
            lambda lock: _patched(lock, "boto3", groups=["dev"]),
            "boto3 is not in the main group",
        ),
        (
            None,
            lambda lock: _patched(lock, "botocore", optional=True),
            "botocore is optional",
        ),
        (
            None,
            lambda lock: _patched(lock, "boto3", markers='sys_platform == "win32"'),
            "boto3 is not installed on linux/py3.11",
        ),
        (
            None,
            lambda lock: _patched(lock, "jmespath", groups=["dev"]),
            "jmespath is not in the main group",
        ),
        (
            None,
            lambda lock: _without(lock, "s3transfer"),
            "0 entries for s3transfer",
        ),
        (
            lambda pyproject: _move_to_dev(pyproject, "boto3"),
            None,
            "boto3 is not a main dependency",
        ),
    ],
    ids=[
        "lock-without-boto3",
        "lock-without-botocore",
        "boto3-dev-only",
        "botocore-optional",
        "boto3-windows-only",
        "transitive-dev-only",
        "transitive-missing",
        "pyproject-dev-group",
    ],
)
def test_detector_rejects_an_image_without_the_aws_sdk(
    mutate_pyproject: Any, mutate_lock: Any, message: str
) -> None:
    pyproject = copy.deepcopy(_toml(PYPROJECT))
    lock = copy.deepcopy(_toml(LOCK))
    if mutate_pyproject is not None:
        pyproject = mutate_pyproject(pyproject)
    if mutate_lock is not None:
        lock = mutate_lock(lock)
    problems = aws_sdk_lock_problems(pyproject, lock)
    assert any(message in problem for problem in problems), problems


def test_pip_requirements_mirror_the_poetry_ranges() -> None:
    dependencies = _toml(PYPROJECT)["tool"]["poetry"]["dependencies"]
    requirements: dict[str, SpecifierSet] = {}
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            requirement = Requirement(line)
            requirements[canonicalize_name(requirement.name)] = requirement.specifier
    for name in AWS_SDK:
        assert requirements.get(name) == SpecifierSet(dependencies[name]), name


def test_aws_sdk_imports_at_the_locked_versions() -> None:
    import boto3
    import botocore
    from botocore.config import Config

    locked: dict[str, str] = {
        str(canonicalize_name(package["name"])): package["version"]
        for package in _toml(LOCK)["package"]
    }
    assert boto3.__version__ == locked["boto3"]
    assert botocore.__version__ == locked["botocore"]
    assert Config(connect_timeout=2).connect_timeout == 2
