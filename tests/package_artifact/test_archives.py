"""Exact wheel, sdist, and distribution metadata contracts."""

from __future__ import annotations

import re
import tarfile
import tomllib
from collections import Counter
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath
from zipfile import ZipFile

from conftest import PACKAGE, ROOT, ArtifactBuild
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

DOMAINS = ("flow", "policy", "prompt", "srm")
ROOT_PAYLOAD = {"__init__.py", "metrics.py", "protocol.py", "py.typed"}
DIST_INFO_PAYLOAD = {"METADATA", "RECORD", "WHEEL"}


def project_metadata() -> dict:  # type: ignore[type-arg]
    """Load the package manifest that defines artifact identity and metadata."""
    with (PACKAGE / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


PROJECT = project_metadata()["tool"]["poetry"]
ARCHIVE_NAME = canonicalize_name(PROJECT["name"]).replace("-", "_")
VERSION = PROJECT["version"]
SDIST_ROOT = f"{ARCHIVE_NAME}-{VERSION}"
DIST_INFO_ROOT = f"{ARCHIVE_NAME}-{VERSION}.dist-info"


def expected_package_payload() -> set[str]:
    """Derive the recursive runtime payload from the reviewed source roots."""
    source = PACKAGE / "neurostack_core"
    expected = {f"neurostack_core/{name}" for name in ROOT_PAYLOAD}
    for domain in DOMAINS:
        expected.update(
            path.relative_to(PACKAGE).as_posix()
            for path in (source / domain).rglob("*.py")
        )
    for pattern in ("*.py", "*.toml"):
        expected.update(
            path.relative_to(PACKAGE).as_posix()
            for path in (source / "presets").rglob(pattern)
        )
    return expected


def metadata_message(raw: bytes):  # type: ignore[no-untyped-def]
    """Parse core metadata while retaining the Markdown payload."""
    return BytesParser(policy=policy.default).parsebytes(raw)


def metadata_description(raw: bytes) -> str:
    """Decode the UTF-8 description body defined by the core metadata spec."""
    _, separator, description = raw.partition(b"\n\n")
    assert separator
    return description.decode("utf-8")


def assert_safe_archive_name(name: str, root: str) -> None:
    """Require a canonical POSIX member below the one expected archive root."""
    path = PurePosixPath(name)
    assert name and "\\" not in name and not name.startswith("/")
    assert path.as_posix() == name
    assert all(part not in {"", ".", ".."} for part in path.parts)
    assert path.parts[0] == root


def normalized_requirement(raw: str) -> tuple[object, ...]:
    """Retain every dependency field while normalizing insignificant spelling."""
    requirement = Requirement(raw)
    return (
        canonicalize_name(requirement.name),
        tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        tuple(sorted(str(item) for item in requirement.specifier)),
        str(requirement.marker) if requirement.marker else None,
        requirement.url,
    )


def pyproject_requirements() -> list[str]:
    """Convert the supported Poetry dependency declarations to PEP 508."""
    dependencies = project_metadata()["tool"]["poetry"]["dependencies"]
    requirements = []
    for name, declaration in dependencies.items():
        if name == "python":
            continue
        if isinstance(declaration, str):
            requirements.append(name if declaration == "*" else f"{name}{declaration}")
            continue
        assert isinstance(declaration, dict), (name, declaration)
        extras = declaration.get("extras", [])
        requirement = name + (f"[{','.join(extras)}]" if extras else "")
        if "url" in declaration:
            requirement += f" @ {declaration['url']}"
        else:
            version = declaration.get("version", "*")
            if version != "*":
                requirement += version
        if declaration.get("markers"):
            requirement += f" ; {declaration['markers']}"
        supported = {"version", "extras", "markers", "url", "optional"}
        assert set(declaration) <= supported, (name, declaration)
        requirements.append(requirement)
    return requirements


def test_wheel_has_the_exact_recursive_payload(
    artifact_build: ArtifactBuild,
) -> None:
    with ZipFile(artifact_build.wheel) as archive:
        member_list = archive.namelist()
        assert len(member_list) == len(set(member_list))
        for name in member_list:
            root = name.split("/", 1)[0]
            assert root in {"neurostack_core", DIST_INFO_ROOT}
            assert_safe_archive_name(name, root)
        members = set(member_list)
        dist_info = {name for name in members if ".dist-info/" in name}
        prefixes = {name.split("/", 1)[0] for name in dist_info}
        assert prefixes == {DIST_INFO_ROOT}
        prefix = DIST_INFO_ROOT
        expected_metadata = {f"{prefix}/{filename}" for filename in DIST_INFO_PAYLOAD}
        assert members == expected_package_payload() | expected_metadata
        assert (
            archive.read("neurostack_core/py.typed")
            == (PACKAGE / "neurostack_core" / "py.typed").read_bytes()
        )
        for name in expected_package_payload():
            assert archive.read(name) == (PACKAGE / name).read_bytes()

    assert not any("tests/" in name or "package_artifact" in name for name in members)
    assert not any(name.startswith("src/") for name in members)


def test_sdist_has_exact_build_and_consumer_inputs(
    artifact_build: ArtifactBuild,
) -> None:
    with tarfile.open(artifact_build.sdist) as archive:
        members = archive.getmembers()
        assert all(member.isfile() or member.isdir() for member in members)
        assert len([member.name for member in members]) == len(
            {member.name for member in members}
        )
        for member in members:
            assert_safe_archive_name(member.name, SDIST_ROOT)
        files = {member.name for member in members if member.isfile()}
    prefixes = {name.split("/", 1)[0] for name in files}
    assert prefixes == {SDIST_ROOT}
    prefix = SDIST_ROOT
    expected = {
        f"{prefix}/README.md",
        f"{prefix}/PKG-INFO",
        f"{prefix}/pyproject.toml",
        f"{prefix}/scripts/check_public_api.py",
        f"{prefix}/scripts/smoke.py",
    }
    expected.update(f"{prefix}/{name}" for name in expected_package_payload())
    assert files == expected
    assert not any("tests/" in name or "package_artifact" in name for name in files)
    with tarfile.open(artifact_build.sdist) as archive:
        source_inputs = {
            "README.md",
            "pyproject.toml",
            "scripts/check_public_api.py",
            "scripts/smoke.py",
            *expected_package_payload(),
        }
        for relative in source_inputs:
            extracted = archive.extractfile(f"{prefix}/{relative}")
            assert extracted is not None
            assert extracted.read() == (PACKAGE / relative).read_bytes()


def test_metadata_has_only_core_dependencies_and_readme(
    artifact_build: ArtifactBuild,
) -> None:
    with ZipFile(artifact_build.wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name)
        message = metadata_message(metadata)

    raw_dependencies = message.get_all("Requires-Dist", [])
    normalized_dependencies = [
        normalized_requirement(value) for value in raw_dependencies
    ]
    expected_dependencies = [
        normalized_requirement(value) for value in pyproject_requirements()
    ]
    assert canonicalize_name(message["Name"]) == "neurostack-core"
    assert message["Version"] == VERSION
    python_constraint = project_metadata()["tool"]["poetry"]["dependencies"]["python"]
    assert SpecifierSet(message["Requires-Python"]) == SpecifierSet(python_constraint)
    assert len(normalized_dependencies) == len(set(normalized_dependencies))
    assert Counter(normalized_dependencies) == Counter(expected_dependencies)
    assert message["Description-Content-Type"] == "text/markdown"
    with tarfile.open(artifact_build.sdist) as archive:
        pkg_info = archive.getmember(f"{SDIST_ROOT}/PKG-INFO")
        extracted = archive.extractfile(pkg_info)
        assert extracted is not None
        sdist_metadata = extracted.read()
        readme_file = archive.extractfile(f"{SDIST_ROOT}/README.md")
        assert readme_file is not None
        archived_readme = readme_file.read().decode("utf-8")
    assert sdist_metadata == metadata
    assert metadata_description(metadata).strip() == archived_readme.strip()


def test_wheel_records_isolated_backend_provenance(
    artifact_build: ArtifactBuild,
) -> None:
    build_requirement = Requirement(project_metadata()["build-system"]["requires"][0])
    assert canonicalize_name(build_requirement.name) == "poetry-core"
    with ZipFile(artifact_build.wheel) as archive:
        wheel = metadata_message(archive.read(f"{DIST_INFO_ROOT}/WHEEL"))
    generator = wheel["Generator"]
    match = re.fullmatch(r"poetry-core (?P<version>\S+)", generator)
    assert match is not None, generator
    assert match.group("version") in build_requirement.specifier


def test_artifacts_are_fresh_and_external_to_persistent_dist(
    artifact_build: ArtifactBuild,
) -> None:
    persistent_dist = (PACKAGE / "dist").resolve()
    assert not artifact_build.wheel.resolve().is_relative_to(persistent_dist)
    assert not artifact_build.sdist.resolve().is_relative_to(persistent_dist)
    assert not artifact_build.workspace.resolve().is_relative_to(ROOT)
