"""Installed-wheel behavior from a clean external consumer environment."""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from conftest import (
    PACKAGE,
    ROOT,
    ArtifactBuild,
    ExternalInstall,
    clean_environment,
    external_environment,
)
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

EXPECTED = ROOT / "tests" / "baselines" / "neurostack-core-extraction-public-api.json"
CHECKER = PACKAGE / "scripts" / "check_public_api.py"
EXTRACTOR = ROOT / "scripts" / "extract-readme-quickstart.py"
FORBIDDEN_APP_DEPENDENCIES = {
    "asyncpg",
    "fastapi",
    "qdrant-client",
    "redis",
    "sqlalchemy",
}


def run_external(
    install: ExternalInstall,
    *arguments: str,
    cwd: Path | None = None,
    empty_environment: bool = False,
    timezone: bool = False,
) -> subprocess.CompletedProcess[str]:
    environment = (
        clean_environment(timezone=timezone)
        if empty_environment
        else external_environment()
    )
    if timezone:
        environment["PYTHONTZPATH"] = ""
    return subprocess.run(
        [str(install.python), *arguments],
        cwd=cwd or install.workspace,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def arrange_checker_inputs(install: ExternalInstall) -> tuple[Path, Path]:
    """Copy immutable checker inputs into this test's private consumer tree."""
    checker = install.workspace / "check_public_api.py"
    fixture = install.workspace / "public-api.json"
    shutil.copy2(CHECKER, checker)
    shutil.copy2(EXPECTED, fixture)
    return checker, fixture


def run_installed_checker(
    install: ExternalInstall, *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    checker, fixture = arrange_checker_inputs(install)
    return run_external(
        install,
        str(checker),
        "--expected",
        str(fixture),
        "--installed",
        cwd=cwd,
    )


def test_source_and_installed_facades_match_exact_fixture(
    external_install: ExternalInstall,
) -> None:
    source = subprocess.run(
        [sys.executable, str(CHECKER), "--expected", str(EXPECTED), "--source"],
        cwd=ROOT,
        env=external_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert source.returncode == 0, source.stdout + source.stderr

    installed = run_installed_checker(external_install)
    assert installed.returncode == 0, installed.stdout + installed.stderr


def test_installed_checker_rejects_a_cwd_shadow(
    external_install: ExternalInstall,
) -> None:
    shadow = external_install.workspace / "shadow"
    (shadow / "neurostack_core").mkdir(parents=True)
    (shadow / "neurostack_core" / "__init__.py").write_text("", encoding="utf-8")
    result = run_installed_checker(external_install, cwd=shadow)
    assert result.returncode == 1
    assert "refuses cwd shadow" in result.stderr


def test_installed_checker_rejects_a_prefix_internal_shadow(
    external_install: ExternalInstall,
) -> None:
    checker, fixture = arrange_checker_inputs(external_install)
    shadow_root = external_install.python.parents[1] / "prefix-shadow"
    shutil.copytree(
        external_install.site_packages / "neurostack_core",
        shadow_root / "neurostack_core",
    )
    code = (
        "import runpy, sys; "
        f"sys.path.insert(0, {str(shadow_root)!r}); "
        f"sys.argv = [{str(checker)!r}, '--expected', {str(fixture)!r}, "
        "'--installed']; "
        f"runpy.run_path({str(checker)!r}, run_name='__main__')"
    )
    result = run_external(external_install, "-c", code)
    assert result.returncode == 1
    assert "expected exact origin" in result.stderr


def test_installed_checker_rejects_record_corruption(
    external_install: ExternalInstall,
) -> None:
    record = external_install.dist_info / "RECORD"
    original = record.read_bytes()
    target = "neurostack_core/flow/__init__.py"

    for corruption, expected_error in (
        ("missing-row", "installed files and RECORD differ"),
        ("hash", "RECORD hash mismatch"),
        ("size", "RECORD size mismatch"),
        ("unsafe", "unsafe RECORD member"),
    ):
        record.write_bytes(original)
        with record.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        if corruption == "missing-row":
            rows = [row for row in rows if row[0] != target]
        elif corruption == "unsafe":
            rows.append(["../prefix-shadow.py", "sha256=AAAA", "1"])
        else:
            row = next(row for row in rows if row[0] == target)
            row[1 if corruption == "hash" else 2] = (
                "sha256=AAAA" if corruption == "hash" else str(int(row[2]) + 1)
            )
        with record.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        result = run_installed_checker(external_install)
        assert result.returncode == 1, corruption
        assert expected_error in result.stderr, (corruption, result.stderr)

    record.write_bytes(original)


def test_installed_checker_rejects_truncated_package_member(
    external_install: ExternalInstall,
) -> None:
    member = external_install.site_packages / "neurostack_core" / "flow" / "__init__.py"
    member.write_bytes(member.read_bytes()[:8])
    result = run_installed_checker(external_install)
    assert result.returncode == 1
    assert "RECORD hash mismatch" in result.stderr


def test_external_install_imports_payload_and_resolves_empty_tzpath(
    external_install: ExternalInstall,
) -> None:
    code = """
from importlib.metadata import distribution
from pathlib import Path
from zoneinfo import ZoneInfo
import neurostack_core
import neurostack_core.flow
import neurostack_core.policy
import neurostack_core.prompt
import neurostack_core.prompt.templates
import neurostack_core.srm

root = Path(neurostack_core.__file__).resolve().parent
assert (root / 'py.typed').is_file()
assert distribution('tzdata').version
assert ZoneInfo('America/Sao_Paulo').key == 'America/Sao_Paulo'
print(root)
"""
    result = run_external(
        external_install,
        "-c",
        code,
        empty_environment=True,
        timezone=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    imported_root = Path(result.stdout.strip()).resolve()
    assert imported_root.is_relative_to(external_install.python.parents[1])
    assert not imported_root.is_relative_to(ROOT)


def test_installed_dependencies_are_only_declared_and_transitive(
    external_install: ExternalInstall,
) -> None:
    code = """
import json
from importlib.metadata import distributions

print(json.dumps({
    dist.metadata['Name']: list(dist.requires or [])
    for dist in distributions()
    if dist.metadata['Name']
}, sort_keys=True))
"""
    result = run_external(external_install, "-c", code)
    assert result.returncode == 0, result.stdout + result.stderr
    raw = json.loads(result.stdout)
    installed = {canonicalize_name(name): requires for name, requires in raw.items()}
    core_name = canonicalize_name("neurostack-core")
    direct = {
        canonicalize_name(Requirement(value).name) for value in installed[core_name]
    }
    assert direct == {"pydantic", "prometheus-client", "tzdata"}
    assert FORBIDDEN_APP_DEPENDENCIES.isdisjoint(installed)

    closure = {core_name}
    pending = [core_name]
    while pending:
        name = pending.pop()
        for raw_requirement in installed[name]:
            requirement = Requirement(raw_requirement)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            dependency = canonicalize_name(requirement.name)
            assert dependency in installed
            if dependency not in closure:
                closure.add(dependency)
                pending.append(dependency)
    assert set(installed) - {"pip", "setuptools"} == closure


def test_smoke_and_readme_quickstart_execute_externally(
    external_install: ExternalInstall,
    artifact_build: ArtifactBuild,
) -> None:
    smoke = external_install.workspace / "smoke.py"
    readme = external_install.workspace / "README.md"
    with tarfile.open(artifact_build.sdist) as archive:
        root = "neurostack_core-0.1.0"
        smoke.write_bytes(archive.extractfile(f"{root}/scripts/smoke.py").read())  # type: ignore[union-attr]
        readme.write_bytes(archive.extractfile(f"{root}/README.md").read())  # type: ignore[union-attr]
    smoke_result = run_external(
        external_install,
        str(smoke),
        empty_environment=True,
    )
    assert smoke_result.returncode == 0, smoke_result.stdout + smoke_result.stderr
    assert (
        smoke_result.stdout.strip()
        == "intent=agendar category=scheduling stage=em_qualificacao"
    )

    quickstart = external_install.workspace / "quickstart.py"
    extracted = subprocess.run(
        [sys.executable, str(EXTRACTOR), str(readme)],
        cwd=external_install.workspace,
        env=external_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert extracted.returncode == 0, extracted.stdout + extracted.stderr
    quickstart.write_text(extracted.stdout, encoding="utf-8")
    quickstart_result = run_external(
        external_install,
        str(quickstart),
        empty_environment=True,
    )
    assert quickstart_result.returncode == 0, (
        quickstart_result.stdout + quickstart_result.stderr
    )
    assert (
        quickstart_result.stdout.strip()
        == "intent=agendar category=scheduling stage=em_qualificacao"
    )
