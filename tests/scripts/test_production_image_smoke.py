"""Contract of the production image smoke (scripts/production_image_smoke.py).

The unit tests pin what the smoke builds and checks without Docker and always
run. The end-to-end test builds the image from the working tree and runs every
check; it is OPT-IN: it runs only with ``RUN_DOCKER_IMAGE_SMOKE=1`` (and then
fails, never skips, when Docker is unusable), because it takes minutes and
needs the build network (Docker Hub, Debian mirrors, PyPI, the npm registry)
unless the build cache already holds every layer.
"""

from __future__ import annotations

import ast
import copy
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts import production_image_smoke as smoke
from src.api.spa import INDEX_FILE
from tests.alembic_graph import current_head, migration_revisions

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
RUNBOOK = ROOT / "docs/runbooks/asaas-deployment.md"
PLANNER = ROOT / "scripts/asaas_production_wiring.py"
LOCK = tomllib.loads((ROOT / "poetry.lock").read_text(encoding="utf-8"))
OPT_IN = "RUN_DOCKER_IMAGE_SMOKE"

SITE_PACKAGES_DIR = "/usr/local/lib/python3.11/site-packages"
EXPECTED_CHECKS = {
    "imports": ("python", "-c", "import src.api.main, boto3, botocore"),
    # The scanned roots are pinned here: a scan of /tmp would pass anything.
    "no-build-toolchain": ("python", "-c", smoke.TOOLCHAIN, "/"),
    "native-libraries": ("python", "-c", smoke.NATIVE_LIBRARIES, "/usr/local"),
    "no-world-writable": (
        "python",
        "-c",
        smoke.WORLD_WRITABLE,
        "/app",
        SITE_PACKAGES_DIR,
    ),
    "alembic-heads": ("alembic", "heads"),
    "spa-index": ("test", "-f", "/app/spa/index.html"),
    "alembic-offline-sql": ("alembic", "upgrade", "032:035", "--sql"),
}
HEALTHCHECK = "curl -f http://localhost:8000/health || exit 1"
COMMAND = (
    "uvicorn",
    "src.api.main:app",
    "--host",
    "0.0.0.0",
    "--port",
    "8000",
    "--workers",
    "4",
    "--no-access-log",
)
# What the serve probe prints for an image that serves as it should.
SERVED = {
    "health": 200,
    "root": 200,
    "root_is_index": True,
    "v1": 401,
    "pid1_uid": 1000,
    "image_uid": 1000,
}
IMAGE_ID = "sha256:" + "ab" * 32


def _checks() -> dict[str, smoke.Check]:
    return {check.name: check for check in smoke.checks("035", LOCK)}


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


# --- What is built and how --------------------------------------------------


def test_build_is_a_local_linux_amd64_load_of_the_production_target() -> None:
    argv = smoke.build_argv(ROOT, "neurostack-api-smoke:t", "abc123")
    assert argv[:3] == ["docker", "buildx", "build"]
    pairs = list(zip(argv, argv[1:]))
    assert ("--platform", "linux/amd64") in pairs
    assert ("--target", "production") in pairs
    assert ("--build-arg", "VCS_REF=abc123") in pairs
    assert ("--tag", "neurostack-api-smoke:t") in pairs
    assert "--load" in argv
    assert argv[-1] == str(ROOT)
    for forbidden in ("--push", "--output", "-o", "--cache-to"):
        assert forbidden not in argv, forbidden


def test_every_check_runs_offline_in_the_built_image() -> None:
    for check in smoke.checks("035", LOCK):
        argv = smoke.run_argv("img:t", check)
        assert argv[:3] == ["docker", "run", "--rm"]
        assert ("--network", "none") in list(zip(argv, argv[1:]))
        assert ("--pull", "never") in list(zip(argv, argv[1:]))
        assert ("--platform", "linux/amd64") in list(zip(argv, argv[1:]))
        image = argv.index("img:t")
        assert tuple(argv[image + 1 :]) == check.argv
        assert "--privileged" not in argv and "--user" not in argv


def test_kms_signer_check_reads_no_credentials_or_instance_metadata() -> None:
    env = dict(_checks()["kms-signers"].env)
    assert env == {
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_CONFIG_FILE": "/nonexistent",
        "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent",
    }
    argv = smoke.run_argv("img:t", _checks()["kms-signers"])
    assert "--env" in argv
    assert not any("AWS_ACCESS_KEY" in item or "AWS_SECRET" in item for item in argv)


# --- The checks ------------------------------------------------------------


def test_required_checks_run_the_exact_commands() -> None:
    checks = _checks()
    assert list(checks) == [
        "imports",
        "site-packages",
        "no-build-toolchain",
        "native-libraries",
        "no-world-writable",
        "src-imports",
        "kms-signers",
        "alembic-heads",
        "spa-index",
        "alembic-offline-sql",
    ]
    for name, argv in EXPECTED_CHECKS.items():
        assert checks[name].argv == argv, name


def test_spa_check_targets_the_directory_the_image_serves() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ENV SPA_DIST_DIR=/app/spa$", text, re.MULTILINE)
    assert re.search(r"public /app/spa$", text, re.MULTILINE)
    assert smoke.SPA_DIST_DIR == "/app/spa"
    assert _checks()["spa-index"].argv[-1] == f"{smoke.SPA_DIST_DIR}/{INDEX_FILE}"


def test_scanned_site_packages_is_where_the_dockerfile_copies_them() -> None:
    # The deps stage's site-packages land exactly where the checks look.
    text = DOCKERFILE.read_text(encoding="utf-8")
    copies = re.findall(
        r"^COPY --from=deps (\S+site-packages) (\S+)$", text, re.MULTILINE
    )
    assert copies == [(smoke.SITE_PACKAGES_DIR, smoke.SITE_PACKAGES_DIR)]
    assert smoke.SITE_PACKAGES_DIR == SITE_PACKAGES_DIR
    assert smoke.SITE_PACKAGES_DIR.startswith(smoke.NATIVE_ROOT + "/")


def test_runbook_lists_every_check_in_the_order_the_smoke_runs_them() -> None:
    text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    listed = text.split("The smoke runs eleven checks, in this order: ", 1)[1]
    names = re.findall(r"`([a-z-]+)`", listed.split("(see", 1)[0])
    assert names == [*_checks(), "serve"]
    # The image's head is a parameter, never the window's 035 written in.
    assert "--expected-head <head of the image>" in text
    assert "--expected-head 0" not in text


def test_offline_window_is_the_runbook_window_of_the_tree() -> None:
    assert re.search(
        rf'^MIGRATIONS_SOURCE_REVISION = "{smoke.WINDOW_FROM}"$',
        PLANNER.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert f"`alembic upgrade {smoke.WINDOW_TO}`" in RUNBOOK.read_text(encoding="utf-8")
    revisions = {revision: down for revision, down in migration_revisions().values()}
    walk: str | None = current_head()
    chain: list[str] = []
    while walk is not None:
        chain.append(walk)
        walk = revisions[walk]
    assert smoke.WINDOW_TO in chain and smoke.WINDOW_FROM in chain
    assert chain.index(smoke.WINDOW_FROM) > chain.index(smoke.WINDOW_TO)


@pytest.mark.parametrize(
    ("stdout", "ok"),
    [
        ("035 (head)\n", True),
        ("  035 (head)  \n\n", True),
        ("034 (head)\n", False),
        ("035\n", False),
        ("035 (head)\n036 (head)\n", False),
        ("035 (head)\nINFO extra\n", False),
        ("035 (effective head)\n", False),
        ("", False),
    ],
)
def test_heads_check_accepts_only_the_single_expected_head(
    stdout: str, ok: bool
) -> None:
    assert (smoke.heads_problem(stdout, "035") is None) is ok
    validate = _checks()["alembic-heads"].validate
    assert (validate(_completed(stdout)) is None) is ok
    assert validate(_completed("035 (head)\n", returncode=1)) is not None


def _sql(*steps: tuple[str, str], begin: str = "BEGIN;", end: str = "COMMIT;") -> str:
    body = [begin, ""]
    for old, new in steps:
        body += [
            f"-- Running upgrade {old} -> {new}",
            "",
            "ALTER TABLE x ADD COLUMN y INTEGER;",
            "",
            f"UPDATE alembic_version SET version_num='{new}' "
            f"WHERE alembic_version.version_num = '{old}';",
            "",
        ]
    return "\n".join([*body, end, ""])


WINDOW = [("032", "033"), ("033", "034"), ("034", "035")]


@pytest.mark.parametrize(
    ("stdout", "ok"),
    [
        (_sql(*WINDOW), True),
        (_sql(*WINDOW, ("035", "036")), False),
        (_sql(*WINDOW[1:]), False),
        (_sql(WINDOW[0], WINDOW[2]), False),
        (_sql(*WINDOW, end=""), False),
        (_sql(*WINDOW, begin=""), False),
        (_sql(*WINDOW) + "BEGIN;\nCOMMIT;\n", False),
        (_sql(*WINDOW).replace("version_num='035'", "version_num='034'"), False),
        ("", False),
    ],
    ids=[
        "window",
        "past-window",
        "late-start",
        "gap",
        "no-commit",
        "no-begin",
        "two-transactions",
        "wrong-final-version",
        "empty",
    ],
)
def test_offline_sql_check_requires_the_whole_window_in_one_transaction(
    stdout: str, ok: bool
) -> None:
    assert (smoke.offline_sql_problem(stdout, "032", "035") is None) is ok
    validate = _checks()["alembic-offline-sql"].validate
    assert (validate(_completed(stdout)) is None) is ok


def test_offline_sql_check_fails_on_a_nonzero_exit() -> None:
    validate = _checks()["alembic-offline-sql"].validate
    assert validate(_completed(_sql(*WINDOW), returncode=1)) is not None


@pytest.mark.parametrize(
    ("name", "completed", "ok"),
    [
        ("imports", _completed("log line\n"), True),
        ("imports", _completed(returncode=1, stderr="ModuleNotFoundError"), False),
        ("spa-index", _completed(), True),
        ("spa-index", _completed(returncode=1), False),
        ("kms-signers", _completed(smoke.KMS_SIGNERS_OUTPUT + "\n"), True),
        ("kms-signers", _completed("Boto3KmsDigestSigner\n"), False),
        ("kms-signers", _completed(smoke.KMS_SIGNERS_OUTPUT, returncode=1), False),
        ("src-imports", _completed("22 top-level modules resolve\n"), True),
        ("src-imports", _completed("0 top-level modules resolve\n"), False),
        ("src-imports", _completed("boto3 (src/x.py)\n", returncode=1), False),
        ("src-imports", _completed("boto3 (src/x.py)\n"), False),
    ],
)
def test_exit_status_and_output_checks(
    name: str, completed: subprocess.CompletedProcess[str], ok: bool
) -> None:
    assert (_checks()[name].validate(completed) is None) is ok


def test_src_import_scan_flags_a_missing_lazy_import(tmp_path: Path) -> None:
    package = tmp_path / "src" / "pkg"
    package.mkdir(parents=True)
    (package / "lazy.py").write_text(
        "def signer():\n    import not_installed_sdk_xyz\n", encoding="utf-8"
    )
    (package / "ok.py").write_text(
        "import json\nfrom src.pkg import lazy\nfrom . import lazy as again\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-c", smoke.SRC_IMPORTS],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout.strip() == "not_installed_sdk_xyz (src/pkg/lazy.py)"


def test_src_import_scan_fails_when_src_is_absent(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", smoke.SRC_IMPORTS],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert not smoke.SRC_IMPORTS_OUTPUT.match(completed.stdout.strip())


# --- site-packages -----------------------------------------------------------

IMAGE_ENVIRONMENT = {
    "implementation_name": "cpython",
    "implementation_version": "3.11.17",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "platform_python_implementation": "CPython",
    "platform_release": "6.6.87",
    "platform_system": "Linux",
    "platform_version": "#1 SMP",
    "python_full_version": "3.11.17",
    "python_version": "3.11",
    "sys_platform": "linux",
}
SITE = "/usr/local/lib/python3.11/site-packages"


def _report(
    *,
    drop: str | None = None,
    extra: list[list[str]] | None = None,
    version: dict[str, str] | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    locked = smoke.locked_main(LOCK, IMAGE_ENVIRONMENT)
    rows = [
        [name, (version or {}).get(name, locked_version), SITE]
        for name, locked_version in locked.items()
        if name != drop
    ]
    tooling = {"pip": "26.2.1", "setuptools": "84.0.0", "wheel": "0.46.3"}
    rows += [
        [name, (version or {}).get(name, tooling_version), SITE]
        for name, tooling_version in tooling.items()
        if name != drop
    ]
    rows += [*(extra or [])]
    return json.dumps(
        {"environment": environment or IMAGE_ENVIRONMENT, "distributions": rows}
    )


def test_locked_main_is_the_main_group_for_the_image_interpreter() -> None:
    locked = smoke.locked_main(LOCK, IMAGE_ENVIRONMENT)
    assert locked["boto3"] == "1.43.115" and locked["botocore"] == "1.43.115"
    assert locked["packaging"] == "26.2"
    assert locked["neurostack-core"] == "0.1.0"
    for tooling in ("poetry", "pip", "setuptools", "pytest", "dulwich"):
        assert tooling not in locked, tooling
    assert len(locked) == 75


def test_locked_main_evaluates_markers_in_the_given_environment() -> None:
    lock = copy.deepcopy(LOCK)
    for package in lock["package"]:
        if package["name"] == "boto3":
            package["markers"] = {"main": 'sys_platform == "win32"'}
    assert "boto3" not in smoke.locked_main(lock, IMAGE_ENVIRONMENT)
    windows = {**IMAGE_ENVIRONMENT, "sys_platform": "win32"}
    assert smoke.locked_main(lock, windows)["boto3"] == "1.43.115"


def test_locked_main_skips_optional_packages() -> None:
    lock = copy.deepcopy(LOCK)
    for package in lock["package"]:
        if package["name"] == "boto3":
            package["optional"] = True
    assert "boto3" not in smoke.locked_main(lock, IMAGE_ENVIRONMENT)


@pytest.mark.parametrize(
    ("stdout", "problem"),
    [
        (_report(), None),
        (
            _report(extra=[["packaging", "26.3", SITE]]),
            "packaging has 2 metadata directories ['26.2', '26.3']",
        ),
        (_report(version={"packaging": "26.3"}), "packaging is ['26.3'], locked 26.2"),
        (_report(drop="boto3"), "boto3 1.43.115 is missing"),
        (
            _report(extra=[["poetry", "2.4.1", SITE]]),
            "poetry ['2.4.1'] is not in the lock's main group",
        ),
        (
            _report(extra=[["dulwich", "1.2.17", SITE]]),
            "dulwich ['1.2.17'] is not in the lock's main group",
        ),
        (
            _report(extra=[["Poetry_Core", "2.4.0", SITE]]),
            "poetry-core ['2.4.0'] is not in the lock's main group",
        ),
        ("not json", "unreadable distribution report"),
        (json.dumps({"distributions": []}), "unreadable distribution report"),
        (
            _report(environment={**IMAGE_ENVIRONMENT, "python_version": "3.12"}),
            "async-timeout ['5.0.1'] is not in the lock's main group",
        ),
        # The fresh runtime image's own pip and setuptools, not the lock's.
        (_report(version={"pip": "24.0"}), "pip is ['24.0'], locked 26.2.1"),
        (
            _report(version={"setuptools": "79.0.1"}),
            "setuptools is ['79.0.1'], locked 84.0.0",
        ),
        (
            _report(extra=[["pip", "24.0", SITE]]),
            "pip has 2 metadata directories ['24.0', '26.2.1']",
        ),
        (_report(version={"wheel": "0.47.0"}), None),
        (_report(drop="wheel"), None),
    ],
    ids=[
        "exact",
        "duplicate-metadata",
        "wrong-version",
        "missing",
        "poetry-shipped",
        "poetry-dependency-shipped",
        "name-normalised",
        "not-json",
        "no-environment",
        "markers-use-the-reported-interpreter",
        "image-pip",
        "image-setuptools",
        "pip-overlay",
        "wheel-is-the-image-s-own",
        "no-wheel",
    ],
)
def test_site_packages_check(stdout: str, problem: str | None) -> None:
    validate = _checks()["site-packages"].validate
    found = validate(_completed(stdout))
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found, found
    assert validate(_completed(_report(), returncode=1)) is not None


def test_locked_tooling_is_the_lock_s_pip_and_setuptools() -> None:
    # wheel is the pinned image's own: the lock does not pin it.
    assert smoke.locked_tooling(LOCK) == {"pip": "26.2.1", "setuptools": "84.0.0"}


def test_site_packages_check_fails_when_nothing_is_locked() -> None:
    problem = smoke.site_packages_problem(_report(), {"package": []})
    assert problem == "poetry.lock has no main-group package for this interpreter"


def test_site_packages_snippet_reports_the_running_interpreter() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", smoke.SITE_PACKAGES],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    assert set(report["environment"]) == set(IMAGE_ENVIRONMENT)
    assert report["environment"]["sys_platform"] == sys.platform
    names = {row[0].lower() for row in report["distributions"]}
    assert {"boto3", "botocore", "pytest"} <= names


# --- No compilers or build headers -----------------------------------------

CLEAN_BINARIES = {
    "/usr/bin": ["bash", "curl", "dpkg", "ldconfig", "ldd", "perl", "sh"],
    "/usr/local/bin": [
        "alembic",
        "pip",
        "python",
        "python3",
        "python3.11-config",
        "uvicorn",
        "wheel",
    ],
}
CLEAN_PACKAGES = [
    "curl",
    "dpkg",
    "gcc-14-base",
    "libc6",
    "libgcc-s1",
    "libpq5",
    "libstdc++6",
    "libudev1",
]
PYTHON_HEADERS = [
    "/usr/local/include/python3.11/Python.h",
    "/usr/local/include/python3.11/cpython/object.h",
]


def _toolchain(**changes: Any) -> str:
    report: dict[str, Any] = {
        "binaries": copy.deepcopy(CLEAN_BINARIES),
        "compiler_dirs": [],
        "headers": list(PYTHON_HEADERS),
        "apt_lists": [],
        "packages": list(CLEAN_PACKAGES),
    }
    report.update(changes)
    return json.dumps(report)


def _with_binary(directory: str, name: str) -> str:
    binaries = copy.deepcopy(CLEAN_BINARIES)
    binaries.setdefault(directory, []).append(name)
    return _toolchain(binaries=binaries)


def test_toolchain_check_passes_a_runtime_image() -> None:
    validate = _checks()["no-build-toolchain"].validate
    assert validate(_completed(_toolchain())) is None
    assert validate(_completed(_toolchain(), returncode=1)) is not None


# Each name alone must fail the check (a check reduced to "gcc" must not pass).
BUILD_TOOLS = [
    "gcc",
    "g++",
    "cc",
    "c++",
    "c89",
    "c99",
    "c89-gcc",
    "cpp",
    "make",
    "gmake",
    "ld",
    "ld.bfd",
    "ld.gold",
    "as",
    "ar",
    "ranlib",
    "nm",
    "objcopy",
    "objdump",
    "strip",
    "readelf",
    "pkg-config",
    "pkgconf",
    "pg_config",
    "clang",
    "gcc-14",
    "g++-14",
    "cpp-14",
    "gcc-ar-14",
    "lto-dump-14",
    "x86_64-linux-gnu-gcc",
    "x86_64-linux-gnu-gcc-14",
    "x86_64-linux-gnu-g++-14",
    "x86_64-linux-gnu-cpp-14",
    "x86_64-linux-gnu-ld",
    "x86_64-linux-gnu-ld.bfd",
    "x86_64-linux-gnu-as",
    "x86_64-linux-gnu-pkg-config",
    "aarch64-linux-gnu-gcc-14",
]


@pytest.mark.parametrize("name", BUILD_TOOLS)
@pytest.mark.parametrize("directory", ["/usr/bin", "/usr/local/bin", "/opt/bin"])
def test_toolchain_check_flags_every_build_tool(name: str, directory: str) -> None:
    problem = smoke.toolchain_problem(_with_binary(directory, name))
    assert problem is not None and f"{directory}/{name}" in problem, problem


@pytest.mark.parametrize(
    "name",
    [
        "ldd",
        "ldconfig",
        "arch",
        "astro",
        "python3.11-config",
        "x86_64-linux-gnu-python3.11-config",
        "make-ssl-cert",
        "ccache-swig",
        "stripe",
    ],
)
def test_toolchain_check_leaves_runtime_binaries_alone(name: str) -> None:
    assert smoke.toolchain_problem(_with_binary("/usr/bin", name)) is None


@pytest.mark.parametrize(
    "package",
    [
        "build-essential",
        "libpq-dev",
        "libc6-dev",
        "linux-libc-dev",
        "libssl-dev",
        "libpython3.11-dev",
        "libstdc++-14-dev",
        "gcc",
        "gcc-14",
        "gcc-14-x86-64-linux-gnu",
        "g++",
        "g++-14",
        "cpp",
        "cpp-14-x86-64-linux-gnu",
        "make",
        "binutils",
        "binutils-common",
        "binutils-x86-64-linux-gnu",
        "libbinutils",
        "pkgconf",
        "pkgconf-bin",
        "pkg-config",
        "dpkg-dev",
        "libc-dev-bin",
        "clang-19",
    ],
)
def test_toolchain_check_flags_every_build_package(package: str) -> None:
    assert smoke.is_build_package(package)
    problem = smoke.toolchain_problem(_toolchain(packages=[*CLEAN_PACKAGES, package]))
    assert problem is not None and f"'{package}'" in problem, problem


@pytest.mark.parametrize(
    "package",
    ["gcc-14-base", "libgcc-s1", "libstdc++6", "libpq5", "udev", "libdevmapper1.02.1"],
)
def test_runtime_packages_are_not_build_packages(package: str) -> None:
    assert not smoke.is_build_package(package)


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        (
            {"headers": [*PYTHON_HEADERS, "/usr/include/stdio.h"]},
            "/usr/include/stdio.h",
        ),
        (
            {"headers": ["/usr/local/include/python3.12/Python.h"]},
            "1 headers outside /usr/local/include/python3.11/",
        ),
        (
            {"headers": ["/usr/include/c++/14/vector.hpp"]},
            "/usr/include/c++/14/vector.hpp",
        ),
        ({"headers": ["/usr/include/c++/14/vector"]}, "/usr/include/c++/14/vector"),
        ({"compiler_dirs": ["/usr/lib/gcc"]}, "compiler directories: ['/usr/lib/gcc']"),
        ({"compiler_dirs": ["/usr/libexec/gcc"]}, "/usr/libexec/gcc"),
        ({"apt_lists": ["lock"]}, "/var/lib/apt/lists holds ['lock']"),
        ({"binaries": {"/usr/local/bin": ["python3"]}}, "lists no /usr/bin"),
        ({"binaries": {"/usr/bin": ["sh"]}}, "no /usr/local/bin/python3"),
        ({"packages": ["curl"]}, "lists no libc6 or dpkg package"),
        ({"binaries": []}, "unreadable toolchain report"),
        ({"packages": None}, "unreadable toolchain report"),
    ],
    ids=[
        "system-header",
        "other-interpreter-headers",
        "c++-header",
        "c++-extensionless-header",
        "gcc-lib-dir",
        "gcc-libexec-dir",
        "apt-lists",
        "no-usr-bin",
        "no-python",
        "no-dpkg-database",
        "binaries-not-a-map",
        "packages-missing",
    ],
)
def test_toolchain_check_problems(changes: dict[str, Any], problem: str) -> None:
    found = smoke.toolchain_problem(_toolchain(**changes))
    assert found is not None and problem in found, found


def test_toolchain_check_rejects_unreadable_output() -> None:
    assert "unreadable" in (smoke.toolchain_problem("not json") or "")
    assert "unreadable" in (smoke.toolchain_problem("{}") or "")


def _status(*packages: tuple[str, str]) -> str:
    return "\n\n".join(
        f"Package: {name}\nStatus: {status}\nDescription: x\n multi-line: y"
        for name, status in packages
    )


def _fake_root(tmp_path: Path, *, toolchain: bool) -> Path:
    root = tmp_path / "root"
    for directory in ("usr/bin", "usr/local/bin", "var/lib/apt/lists"):
        (root / directory).mkdir(parents=True)
    (root / "bin").symlink_to("usr/bin")
    for binary in ("usr/bin/sh", "usr/bin/dpkg", "usr/local/bin/python3"):
        (root / binary).write_text("", encoding="utf-8")
    python = root / "usr/local/include/python3.11"
    python.mkdir(parents=True)
    (python / "Python.h").write_text("", encoding="utf-8")
    installed = [("libc6", "install ok installed"), ("dpkg", "install ok installed")]
    installed.append(("libpq-dev", "deinstall ok config-files"))
    if toolchain:
        (root / "usr/bin/x86_64-linux-gnu-gcc-14").write_text("", encoding="utf-8")
        (root / "usr/include/c++/14").mkdir(parents=True)
        (root / "usr/include/stdio.h").write_text("", encoding="utf-8")
        # The C++ standard headers have no extension.
        (root / "usr/include/c++/14/vector").write_text("", encoding="utf-8")
        (root / "usr/include/c++/14/bits.tcc").write_text("", encoding="utf-8")
        (root / "usr/libexec/gcc/x86_64-linux-gnu/14").mkdir(parents=True)
        (root / "var/lib/apt/lists/lock").write_text("", encoding="utf-8")
        installed.append(("build-essential", "install ok installed"))
    (root / "var/lib/dpkg").mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text(_status(*installed), encoding="utf-8")
    return root


def _scan(root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", smoke.TOOLCHAIN, str(root)],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PATH": "/usr/local/bin:/bin:/usr/bin"},
    )
    report: dict[str, Any] = json.loads(completed.stdout)
    return report


def test_toolchain_snippet_scans_a_clean_root(tmp_path: Path) -> None:
    report = _scan(_fake_root(tmp_path, toolchain=False))
    # /bin is a symlink to usr/bin: scanned once, under its real path.
    assert report["binaries"] == {
        "/usr/local/bin": ["python3"],
        "/usr/bin": ["dpkg", "sh"],
    }
    assert report["headers"] == ["/usr/local/include/python3.11/Python.h"]
    assert report["compiler_dirs"] == [] and report["apt_lists"] == []
    # A removed package whose configuration files remain is not installed.
    assert report["packages"] == ["dpkg", "libc6"]
    assert smoke.toolchain_problem(json.dumps(report)) is None


def test_toolchain_snippet_finds_tools_headers_lists_and_packages(
    tmp_path: Path,
) -> None:
    report = _scan(_fake_root(tmp_path, toolchain=True))
    problem = smoke.toolchain_problem(json.dumps(report)) or ""
    for found in (
        "/usr/bin/x86_64-linux-gnu-gcc-14",
        "/usr/libexec/gcc",
        "3 headers outside",
        "/usr/include/stdio.h",
        "/usr/include/c++/14/vector",
        "/usr/include/c++/14/bits.tcc",
        "/var/lib/apt/lists holds ['lock']",
        "'build-essential'",
    ):
        assert found in problem, (found, problem)


def test_toolchain_snippet_needs_the_root_to_scan() -> None:
    # No default root: the check names "/" itself (EXPECTED_CHECKS pins it).
    completed = subprocess.run(
        [sys.executable, "-c", smoke.TOOLCHAIN],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0


def test_toolchain_snippet_fails_without_a_dpkg_database(tmp_path: Path) -> None:
    root = _fake_root(tmp_path, toolchain=False)
    (root / "var/lib/dpkg/status").unlink()
    completed = subprocess.run(
        [sys.executable, "-c", smoke.TOOLCHAIN, str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert _checks()["no-build-toolchain"].validate(completed) is not None


# --- Native libraries --------------------------------------------------------

TKINTER = (
    "/usr/local/lib/python3.11/lib-dynload/_tkinter.cpython-311-x86_64-linux-gnu.so"
)
EXTENSION = (
    f"{SITE_PACKAGES_DIR}/asyncpg/protocol/protocol.cpython-311-x86_64-linux-gnu.so"
)


def _native(**changes: Any) -> str:
    report: dict[str, Any] = {
        "elf": [
            "/usr/local/bin/python3.11",
            "/usr/local/lib/libpython3.11.so.1.0",
            TKINTER,
            EXTENSION,
            f"{SITE_PACKAGES_DIR}/numpy.libs/libscipy_openblas64_-32a4b2a6.so",
        ],
        "unresolved": {TKINTER: ["libtcl8.6.so", "libtk8.6.so"]},
        "versions": {},
        "errors": {},
    }
    report.update(changes)
    return json.dumps(report)


# What ldd prints (on stdout, exit status 0) for a library that is present but
# older than the symbol version a file needs.
TOO_OLD = (
    f"{EXTENSION}: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found"
    f" (required by {EXTENSION})"
)


def test_native_libraries_pass_with_only_tkinter_unresolved() -> None:
    validate = _checks()["native-libraries"].validate
    assert validate(_completed(_native())) is None
    assert validate(_completed(_native(unresolved={}))) is None
    assert validate(_completed(_native(), returncode=1)) is not None


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        (
            {"unresolved": {EXTENSION: ["libpq.so.5"]}},
            f"1 ELF files with unresolved libraries: {{'{EXTENSION}': ['libpq.so.5']}}",
        ),
        (
            {"unresolved": {TKINTER: ["libtk8.6.so", "libX11.so.6"]}},
            "1 ELF files with unresolved libraries",
        ),
        (
            {
                "unresolved": {
                    f"{SITE_PACKAGES_DIR}/_tkinter.cpython-311-x86_64-linux-gnu.so": [
                        "libtk8.6.so"
                    ]
                }
            },
            "1 ELF files with unresolved libraries",
        ),
        (
            {"unresolved": {"/usr/local/bin/python3.11": ["libssl.so.3"]}},
            "/usr/local/bin/python3.11",
        ),
        ({"errors": {EXTENSION: "ldd: exited with unknown exit code (139)"}}, "ldd"),
        (
            {"versions": {EXTENSION: [TOO_OLD]}},
            f"1 ELF files need a symbol version their library lacks: "
            f"{{'{EXTENSION}': [{TOO_OLD!r}]}}",
        ),
        (
            # The _tkinter exception covers Tk and Tcl being absent, nothing else.
            {"versions": {TKINTER: [TOO_OLD.replace(EXTENSION, TKINTER)]}},
            "1 ELF files need a symbol version",
        ),
        (
            {
                "unresolved": {},
                "versions": {"/usr/local/bin/python3.11": [TOO_OLD]},
            },
            "need a symbol version",
        ),
        ({"elf": [EXTENSION]}, "no /usr/local/bin/python3.11"),
        ({"elf": ["/usr/local/bin/python3.11"]}, "no site-packages ELF file"),
        ({"elf": []}, "no /usr/local/bin/python3.11"),
        ({"unresolved": []}, "unreadable native library report"),
        ({"versions": []}, "unreadable native library report"),
        ({"versions": None}, "unreadable native library report"),
        ({"errors": None}, "unreadable native library report"),
    ],
    ids=[
        "extension-gap",
        "tkinter-needs-more",
        "tkinter-name-elsewhere",
        "interpreter-gap",
        "ldd-failed",
        "symbol-version-missing",
        "tkinter-symbol-version",
        "interpreter-symbol-version",
        "no-interpreter",
        "no-extension",
        "nothing-scanned",
        "unresolved-not-a-map",
        "versions-not-a-map",
        "versions-missing",
        "errors-missing",
    ],
)
def test_native_libraries_problems(changes: dict[str, Any], problem: str) -> None:
    found = smoke.native_libraries_problem(_native(**changes))
    assert found is not None and problem in found, found


def test_native_libraries_reject_unreadable_output() -> None:
    assert "unreadable" in (smoke.native_libraries_problem("not json") or "")
    assert "unreadable" in (smoke.native_libraries_problem("{}") or "")
    # A snippet that stopped reporting symbol versions fails closed.
    without_versions = json.loads(_native())
    del without_versions["versions"]
    found = smoke.native_libraries_problem(json.dumps(without_versions))
    assert "unreadable" in (found or "")


def _imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.partition(".")[0])
    return names


def test_nothing_in_the_image_imports_tkinter() -> None:
    # The one native-libraries exception holds only while nobody loads it.
    sources = [*(ROOT / "src").rglob("*.py"), *(ROOT / "packages").rglob("*.py")]
    sources += [*(ROOT / "alembic").rglob("*.py"), ROOT / "scripts/bootstrap_admin.py"]
    assert len(sources) > 100
    tk = {"tkinter", "_tkinter", "turtle", "idlelib"}
    offenders = [str(path) for path in sources if _imports_of(path) & tk]
    assert offenders == []


def _elf_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A /usr/local-like tree: a working ELF, a site-packages extension needing
    a missing library, a symlink to the first and a non-ELF file."""

    true = Path(os.path.realpath(shutil.which("true") or "/bin/true"))
    root = tmp_path / "local"
    (root / "bin").mkdir(parents=True)
    good = root / "bin" / "good"
    shutil.copy2(true, good)
    data = true.read_bytes()
    assert b"libc.so.6\x00" in data, "the host's true(1) does not link glibc"
    package = root / "lib/python3.11/site-packages/pkg"
    package.mkdir(parents=True)
    broken = package / "ext.cpython-311-x86_64-linux-gnu.so"
    broken.write_bytes(data.replace(b"libc.so.6\x00", b"libq.so.6\x00"))
    broken.chmod(0o755)
    (root / "bin" / "alias").symlink_to("good")
    (root / "bin" / "script").write_text("#!/bin/sh\n", encoding="utf-8")
    return root, good, broken


@pytest.mark.skipif(shutil.which("ldd") is None, reason="ldd unavailable")
def test_native_libraries_snippet_finds_an_unresolved_library(tmp_path: Path) -> None:
    root, good, broken = _elf_root(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-c", smoke.NATIVE_LIBRARIES, str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    # Symlinks and non-ELF files are not scanned twice or at all.
    assert report["elf"] == sorted([str(good), str(broken)])
    assert report["unresolved"] == {str(broken): ["libq.so.6"]}
    assert report["versions"] == {} and report["errors"] == {}


@pytest.mark.skipif(shutil.which("ldd") is None, reason="ldd unavailable")
def test_native_libraries_snippet_finds_a_library_too_old_for_a_symbol(
    tmp_path: Path,
) -> None:
    # The library is there (ldd resolves it and exits 0) but lacks the symbol
    # version the file needs: the loader would refuse the file at import time.
    root, good, broken = _elf_root(tmp_path)
    data = good.read_bytes()
    assert b"GLIBC_2.2.5\x00" in data, "the host's true(1) needs no GLIBC_2.2.5"
    broken.write_bytes(data.replace(b"GLIBC_2.2.5\x00", b"GLIBC_9.2.5\x00"))
    ldd = subprocess.run(["ldd", str(broken)], capture_output=True, text=True)
    assert ldd.returncode == 0 and "=> not found" not in ldd.stdout, ldd
    completed = subprocess.run(
        [sys.executable, "-c", smoke.NATIVE_LIBRARIES, str(root)],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    assert report["elf"] == sorted([str(good), str(broken)])
    assert report["unresolved"] == {} and report["errors"] == {}
    assert list(report["versions"]) == [str(broken)]
    [line] = report["versions"][str(broken)]
    assert "version `GLIBC_9.2.5' not found" in line, line
    assert line.startswith(f"{broken}: "), line
    problem = smoke.native_libraries_problem(_native(versions=report["versions"]))
    assert problem is not None and "need a symbol version" in problem, problem
    assert "GLIBC_9.2.5" in problem


# What a fake ldd prints for a file: (stdout, stderr, exit status).
LddAnswer = tuple[str, str, int]
CLEAN_LDD: LddAnswer = (
    "\tlinux-vdso.so.1 (0x00007ffd5f5e6000)\n"
    "\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f1c2a000000)\n"
    "\t/lib64/ld-linux-x86-64.so.2 (0x00007f1c2a3f1000)\n",
    "",
    0,
)


def _fake_ldd(directory: Path, answers: dict[str, LddAnswer]) -> dict[str, str]:
    """An ``ldd`` first in PATH that answers by the file's base name (unknown
    names get CLEAN_LDD); returns the environment that puts it there."""

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    script = directory / "ldd"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "here = os.path.dirname(os.path.abspath(__file__))\n"
        "with open(os.path.join(here, 'answers.json'), encoding='utf-8') as f:\n"
        "    answers = json.load(f)\n"
        f"default = {list(CLEAN_LDD)!r}\n"
        "out, err, status = answers.get(os.path.basename(sys.argv[1]), default)\n"
        "sys.stdout.write(out)\n"
        "sys.stderr.write(err)\n"
        "sys.exit(status)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return {**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


def _usr_local(root: Path, *, interpreter: str = "bin/python3.11") -> Path:
    """A /usr/local-shaped tree of ELF-looking files (ldd is faked): the
    interpreter and one site-packages extension."""

    for relative in (interpreter, "lib/python3.11/site-packages/pkg/ext.so"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(b"\x7fELF\x02\x01\x01" + b"\x00" * 57)
    return root


def _native_scan(root: Path, env: dict[str, str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-c", smoke.NATIVE_LIBRARIES, str(root)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    report: dict[str, Any] = json.loads(completed.stdout)
    return report


def _in_image(report: dict[str, Any], root: Path, image_root: str) -> str:
    """The report as the image would print it: the fake root replaced by the
    directory the check scans in the image."""

    return json.dumps(report).replace(str(root), image_root)


_EXT = "ext.so"
_VERSION_LINE = (
    "./ext.so: /usr/lib/x86_64-linux-gnu/libstdc++.so.6: version `GLIBCXX_3.4.32'"
    " not found (required by ./ext.so)"
)


@pytest.mark.parametrize(
    ("answer", "key", "expected", "problem"),
    [
        pytest.param(
            (_VERSION_LINE + "\n" + CLEAN_LDD[0], "", 0),
            "versions",
            [_VERSION_LINE],
            "need a symbol version",
            id="glibcxx",
        ),
        pytest.param(
            (
                "\t/usr/lib/x86_64-linux-gnu/libstdc++.so.6: version `CXXABI_1.3.15'"
                " not found (required by ./ext.so)\n" + CLEAN_LDD[0],
                "",
                0,
            ),
            "versions",
            [
                "/usr/lib/x86_64-linux-gnu/libstdc++.so.6: version `CXXABI_1.3.15'"
                " not found (required by ./ext.so)"
            ],
            "need a symbol version",
            id="cxxabi-indented",
        ),
        pytest.param(
            (
                CLEAN_LDD[0],
                "./ext.so: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38'"
                " not found (required by ./ext.so)\n",
                0,
            ),
            "versions",
            [
                "./ext.so: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38'"
                " not found (required by ./ext.so)"
            ],
            "need a symbol version",
            id="glibc-on-stderr",
        ),
        pytest.param(
            (
                _VERSION_LINE
                + "\n"
                + _VERSION_LINE.replace("GLIBCXX_3.4.32", "GLIBCXX_3.4.31")
                + "\n",
                "",
                0,
            ),
            "versions",
            sorted(
                [
                    _VERSION_LINE,
                    _VERSION_LINE.replace("GLIBCXX_3.4.32", "GLIBCXX_3.4.31"),
                ]
            ),
            "need a symbol version",
            id="two-versions",
        ),
        pytest.param(
            ("", "ldd: exited with unknown exit code (139)\n", 1),
            "errors",
            "ldd: exited with unknown exit code (139)",
            "ldd failed on 1 files",
            id="ldd-fails",
        ),
        pytest.param(
            ("ldd: ./ext.so: Permission denied\n", "", 1),
            "errors",
            "ldd: ./ext.so: Permission denied",
            "ldd failed on 1 files",
            id="ldd-fails-on-stdout",
        ),
        pytest.param(
            ("\tlibq.so.6 => not found\n", "", 0),
            "unresolved",
            ["libq.so.6"],
            "unresolved libraries",
            id="missing-library",
        ),
        pytest.param(CLEAN_LDD, None, None, None, id="vdso-and-resolved"),
        pytest.param(
            ("\tnot a dynamic executable\n", "", 1), None, None, None, id="static"
        ),
        pytest.param(
            ("\tstatically linked\n", "", 0), None, None, None, id="static-pie"
        ),
        pytest.param(
            # A library whose file name merely contains the words.
            (
                "\tlinux-vdso.so.1 (0x00007ffd5f5e6000)\n"
                "\tlibversion-not-found.so.1 => /usr/local/lib/"
                "libversion-not-found.so.1 (0x00007f1c2a000000)\n",
                "",
                0,
            ),
            None,
            None,
            None,
            id="words-in-a-library-name",
        ),
    ],
)
def test_native_libraries_snippet_reads_what_ldd_prints(
    tmp_path: Path,
    answer: LddAnswer,
    key: str | None,
    expected: Any,
    problem: str | None,
) -> None:
    root = _usr_local(tmp_path / "local")
    env = _fake_ldd(tmp_path / "fake-bin", {_EXT: answer})
    report = _native_scan(root, env)
    extension = str(root / "lib/python3.11/site-packages/pkg" / _EXT)
    assert report["elf"] == sorted([str(root / "bin/python3.11"), extension])
    findings = {name: report[name] for name in ("unresolved", "versions", "errors")}
    if key is None:
        assert findings == {"unresolved": {}, "versions": {}, "errors": {}}
    else:
        assert findings.pop(key) == {extension: expected}
        assert findings == {name: {} for name in findings}
    # Through the real check, as the image would report it.
    validate = _checks()["native-libraries"].validate
    found = validate(_completed(_in_image(report, root, smoke.NATIVE_ROOT)))
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found, found


@pytest.mark.parametrize(
    "interpreter", ["bin/python3.11", "../bin/python3.11", "lib/python3.11/x"]
)
def test_native_scan_root_and_interpreter_guard_hold_together(
    tmp_path: Path, interpreter: str
) -> None:
    # Independent of EXPECTED_CHECKS: the root the check passes, scanned in a
    # fake filesystem, must find the interpreter and an extension where the
    # image has them, and the guard must fail a scan that misses the
    # interpreter. A changed root, a dropped guard or both fail here.
    filesystem = tmp_path / "fs"
    image_root = _checks()["native-libraries"].argv[-1]
    _usr_local(filesystem / "usr/local", interpreter=interpreter)
    scanned = filesystem / image_root.lstrip("/")
    env = _fake_ldd(tmp_path / "fake-bin", {})
    report = _native_scan(scanned, env)
    stdout = json.dumps(report).replace(str(filesystem), "")
    found = _checks()["native-libraries"].validate(_completed(stdout))
    if interpreter == "bin/python3.11":
        assert found is None
    else:
        assert found is not None and "no /usr/local/bin/python3.11" in found, found


@pytest.mark.parametrize("python3", [True, False])
def test_toolchain_scan_root_and_guard_hold_together(
    tmp_path: Path, python3: bool
) -> None:
    # Same for no-build-toolchain: the check's root, scanned in a fake root,
    # passes; without /usr/local/bin/python3 the guard must fail it.
    filesystem = _fake_root(tmp_path, toolchain=False)
    if not python3:
        (filesystem / "usr/local/bin/python3").unlink()
    image_root = _checks()["no-build-toolchain"].argv[-1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            smoke.TOOLCHAIN,
            str(filesystem / image_root.lstrip("/")),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": "/usr/local/bin:/bin:/usr/bin"},
    )
    found = _checks()["no-build-toolchain"].validate(completed)
    if python3:
        assert found is None, found
    else:
        assert found is not None and "/usr/local/bin/python3" in found, found


# --- World-writable paths -----------------------------------------------------


def _modes(
    app: list[str] | None = None, site: list[str] | None = None, app_scanned: int = 320
) -> str:
    report: dict[str, Any] = {
        "/app": {"scanned": app_scanned, "writable": app or []},
        SITE_PACKAGES_DIR: {"scanned": 9300, "writable": site or []},
    }
    return json.dumps(report)


def test_world_writable_check_passes_normal_modes() -> None:
    validate = _checks()["no-world-writable"].validate
    assert validate(_completed(_modes())) is None
    assert validate(_completed(_modes(), returncode=1)) is not None


@pytest.mark.parametrize(
    ("report", "problem"),
    [
        (_modes(app=["/app/src/api/main.py"]), "1 world-writable paths under /app"),
        (_modes(app=["/app/src"]), "/app/src"),
        (
            _modes(site=[f"{SITE_PACKAGES_DIR}/boto3/__init__.py"]),
            f"1 world-writable paths under {SITE_PACKAGES_DIR}",
        ),
        (_modes(app=["/app/x"]), "build from a Linux-filesystem clone"),
        (
            _modes(app_scanned=0),
            "nothing scanned under /app",
        ),
        (
            json.dumps({"/app": {"scanned": 1, "writable": []}}),
            "unreadable mode report",
        ),
        ("not json", "unreadable mode report"),
    ],
    ids=["file", "directory", "site-packages", "hint", "empty", "root-missing", "junk"],
)
def test_world_writable_problems(report: str, problem: str) -> None:
    found = smoke.world_writable_problem(report)
    assert found is not None and problem in found, found


def test_world_writable_snippet_reports_files_and_directories(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    (app / "src" / "ok.py").write_text("", encoding="utf-8")
    (app / "src" / "ok.py").chmod(0o644)
    (app / "open.py").write_text("", encoding="utf-8")
    (app / "open.py").chmod(0o666)
    (app / "shared").mkdir()
    (app / "shared").chmod(0o777)
    # Only the "other" write bit counts: group-writable passes, and other-
    # writable without group write fails.
    (app / "group.py").write_text("", encoding="utf-8")
    (app / "group.py").chmod(0o664)
    (app / "other.py").write_text("", encoding="utf-8")
    (app / "other.py").chmod(0o646)
    (app / "group-dir").mkdir()
    (app / "group-dir").chmod(0o775)
    (app / "other-dir").mkdir()
    (app / "other-dir").chmod(0o757)
    (app / "src").chmod(0o755)
    app.chmod(0o755)
    (app / "link").symlink_to("open.py")  # a symlink's own mode is always 0777
    missing = tmp_path / "missing"
    completed = subprocess.run(
        [sys.executable, "-c", smoke.WORLD_WRITABLE, str(app), str(missing)],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    assert report[str(app)] == {
        "scanned": 9,
        "writable": sorted(
            str(app / name) for name in ("open.py", "shared", "other.py", "other-dir")
        ),
    }
    assert report[str(missing)] == {"scanned": 0, "writable": []}


# --- Serving from the image ---------------------------------------------------


def test_serve_container_runs_the_image_command_offline_as_its_user() -> None:
    argv = smoke.serve_argv("img:t", "probe-name", "k" * 64, COMMAND)
    pairs = list(zip(argv, argv[1:]))
    assert argv[:3] == ["docker", "run", "--detach"]
    assert ("--name", "probe-name") in pairs
    assert ("--network", "none") in pairs and ("--pull", "never") in pairs
    assert ("--platform", "linux/amd64") in pairs
    assert ("--env", "ENVIRONMENT=staging") in pairs
    assert ("--env", "SECRET_KEY=" + "k" * 64) in pairs
    image = argv.index("img:t")
    assert argv[image + 1 :] == [*COMMAND, "--lifespan", "off"]
    for forbidden in ("--user", "-u", "--privileged", "-p", "--publish", "-v"):
        assert forbidden not in argv[:image], forbidden
    assert not any(item.startswith(("--volume", "--mount")) for item in argv)


def test_serve_url_is_the_port_the_image_serves() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert smoke.SERVE_URL == "http://127.0.0.1:8000"
    assert '"--port", "8000"' in text.rsplit("FROM ", 1)[1]
    assert "\nEXPOSE 8000\n" in text.rsplit("FROM ", 1)[1]


@pytest.mark.parametrize(
    ("test", "argv"),
    [
        (["CMD-SHELL", HEALTHCHECK], ["/bin/sh", "-c", HEALTHCHECK]),
        (["CMD", "curl", "-f", "http://x/health"], ["curl", "-f", "http://x/health"]),
        (["NONE"], None),
        (["CMD"], None),
        ([], None),
        (None, None),
    ],
    ids=["shell", "exec", "disabled", "empty-exec", "empty", "absent"],
)
def test_healthcheck_argv(test: list[str] | None, argv: list[str] | None) -> None:
    image = {"Config": {"Healthcheck": None if test is None else {"Test": test}}}
    assert smoke.healthcheck_argv(image) == argv


def test_command_argv() -> None:
    assert smoke.command_argv(_image()) == list(COMMAND)
    assert smoke.command_argv(_image(Config={"Cmd": None})) is None
    assert smoke.command_argv(_image(Config={"Cmd": []})) is None
    assert smoke.command_argv({}) is None


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({}, None),
        ({"health": None}, "health is None, expected 200"),
        ({"health": 503}, "health is 503, expected 200"),
        ({"root": 404}, "root is 404, expected 200"),
        ({"root_is_index": False}, "root_is_index is False, expected True"),
        ({"root_is_index": 1}, "root_is_index is 1, expected True"),
        ({"v1": 200}, "v1 is 200, expected 401"),
        ({"v1": 500}, "v1 is 500, expected 401"),
        ({"health": 200.0}, "health is 200.0, expected 200"),
        ({"pid1_uid": 0, "image_uid": 0}, "PID 1 runs as uid 0"),
        ({"pid1_uid": 0}, "PID 1 runs as uid 0, not neurostack (1000)"),
        ({"pid1_uid": 1001}, "PID 1 runs as uid 1001"),
        ({"pid1_uid": None}, "PID 1 runs as uid None"),
    ],
    ids=[
        "served",
        "never-up",
        "unhealthy",
        "no-spa",
        "other-body",
        "truthy-not-true",
        "auth-bypassed",
        "api-error",
        "float-status",
        "root-image-user",
        "root-process",
        "other-user",
        "no-uid",
    ],
)
def test_serve_report_problems(changes: dict[str, Any], problem: str | None) -> None:
    found = smoke.serve_problem(json.dumps({**SERVED, **changes}))
    if problem is None:
        assert found is None
    else:
        assert found is not None and problem in found, found


@pytest.mark.parametrize("stdout", ["", "not json", "[]", json.dumps({"health": 200})])
def test_serve_report_must_be_readable(stdout: str) -> None:
    assert "unreadable serve report" in (smoke.serve_problem(stdout) or "")


def _serve_with(
    monkeypatch: pytest.MonkeyPatch, docker: FakeDocker, **kwargs: Any
) -> dict[str, Any]:
    monkeypatch.setattr(smoke, "_run", docker)
    healthcheck = kwargs.get("healthcheck", ["/bin/sh", "-c", HEALTHCHECK])
    command = kwargs.get("command", list(COMMAND))
    return smoke.serve_check(IMAGE_ID, command, healthcheck)


def _steps(docker: FakeDocker) -> list[str]:
    steps = []
    for call in docker.calls:
        if call[:3] == ["docker", "run", "--detach"]:
            steps.append("start")
        elif call[:2] == ["docker", "exec"]:
            steps.append("probe" if smoke.SERVE_PROBE in call else "healthcheck")
        elif call[:2] in (["docker", "logs"], ["docker", "rm"]):
            steps.append(call[1])
    return steps


def _secret(docker: FakeDocker) -> str:
    start = next(c for c in docker.calls if c[:3] == ["docker", "run", "--detach"])
    return next(
        item.split("=", 1)[1] for item in start if item.startswith("SECRET_KEY=")
    )


def test_serve_check_passes_and_removes_its_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = FakeDocker(_image(Id=IMAGE_ID))
    result = _serve_with(monkeypatch, docker)
    assert result["name"] == "serve"
    assert result["status"] == "pass", result
    assert result["detail"] is None
    assert _steps(docker) == ["start", "probe", "healthcheck", "rm"]
    start, probe, health, removed = docker.calls
    name = start[start.index("--name") + 1]
    assert name.startswith("neurostack-smoke-serve-")
    assert removed == ["docker", "rm", "--force", name]
    assert probe[:3] == ["docker", "exec", name]
    assert probe[-4:] == ["http://127.0.0.1:8000", "/app/spa/index.html"] + [
        "neurostack",
        "90",
    ]
    assert health == ["docker", "exec", name, "/bin/sh", "-c", HEALTHCHECK]
    secret = _secret(docker)
    assert len(secret) >= 32 and secret not in json.dumps(result)


def test_serve_check_names_and_keys_are_fresh_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = FakeDocker(_image(Id=IMAGE_ID)), FakeDocker(_image(Id=IMAGE_ID))
    _serve_with(monkeypatch, first)
    _serve_with(monkeypatch, second)
    assert first.calls[0][first.calls[0].index("--name") + 1] != (
        second.calls[0][second.calls[0].index("--name") + 1]
    )
    assert _secret(first) != _secret(second)


@pytest.mark.parametrize(
    ("answers", "steps", "problem"),
    [
        (
            {"start": _completed(returncode=125, stderr="no such image")},
            ["start", "rm"],
            "the container did not start: no such image",
        ),
        (
            {"probe": _completed(json.dumps({**SERVED, "health": None}))},
            ["start", "probe", "logs", "rm"],
            "health is None, expected 200",
        ),
        (
            {"probe": _completed(returncode=137, stderr="killed")},
            ["start", "probe", "logs", "rm"],
            "exit status 137",
        ),
        (
            {"healthcheck": _completed(returncode=1, stderr="curl: (7)")},
            ["start", "probe", "healthcheck", "logs", "rm"],
            "HEALTHCHECK exited 1: curl: (7)",
        ),
        (
            {"probe": subprocess.TimeoutExpired(["docker", "exec", "x"], 300)},
            ["start", "probe", "rm"],
            "timed out",
        ),
        (
            {"rm": _completed(returncode=1, stderr="permission denied")},
            ["start", "probe", "healthcheck", "rm"],
            "not removed: permission denied",
        ),
        (
            {"rm": subprocess.TimeoutExpired(["docker", "rm"], 300)},
            ["start", "probe", "healthcheck", "rm"],
            "timed out",
        ),
    ],
    ids=[
        "start-failed",
        "never-healthy",
        "probe-killed",
        "healthcheck-failed",
        "probe-timeout",
        "not-removed",
        "removal-timeout",
    ],
)
def test_serve_check_failures_still_remove_the_container(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, Any],
    steps: list[str],
    problem: str,
) -> None:
    docker = FakeDocker(_image(Id=IMAGE_ID), answers=answers)
    result = _serve_with(monkeypatch, docker)
    assert result["status"] == "fail"
    assert problem in result["detail"], result["detail"]
    assert _steps(docker) == steps
    if "logs" in steps:
        assert "container log:\nuvicorn log line" in result["detail"]
    assert _secret(docker) not in result["detail"]


def test_serve_check_tolerates_a_container_that_never_existed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gone = _completed(returncode=1, stderr="Error: No such container: x")
    docker = FakeDocker(
        _image(Id=IMAGE_ID),
        answers={"start": _completed(returncode=125, stderr="bad"), "rm": gone},
    )
    result = _serve_with(monkeypatch, docker)
    assert result["detail"] == "the container did not start: bad"


@pytest.mark.parametrize(
    ("kwargs", "problem"),
    [
        ({"healthcheck": None}, "the image declares no HEALTHCHECK command"),
        ({"command": None}, "the image declares no CMD"),
    ],
)
def test_serve_check_needs_the_image_command_and_healthcheck(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], problem: str
) -> None:
    docker = FakeDocker(_image(Id=IMAGE_ID))
    result = _serve_with(monkeypatch, docker, **kwargs)
    assert result["status"] == "fail" and result["detail"] == problem
    assert _steps(docker) == ["rm"]


class _Stub(BaseHTTPRequestHandler):
    """/health fails ``unhealthy`` times, then answers 200; / and /v1."""

    routes: dict[str, tuple[int, bytes]] = {}
    unhealthy = 0

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        status, body = self.routes.get(self.path, (404, b"missing"))
        if self.path == "/health" and type(self).unhealthy > 0:
            type(self).unhealthy -= 1
            status = 503
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return None


def _probe(
    tmp_path: Path,
    routes: Callable[[Path], dict[str, tuple[int, bytes]]],
    unhealthy: int = 0,
) -> dict[str, Any]:
    index = tmp_path / "index.html"
    index.write_bytes(b"<!doctype html><div id=root></div>")
    handler = type(
        "Handler", (_Stub,), {"routes": routes(index), "unhealthy": unhealthy}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                smoke.SERVE_PROBE,
                f"http://127.0.0.1:{server.server_address[1]}",
                str(index),
                pwd.getpwuid(os.getuid()).pw_name,
                "20",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()
    report: dict[str, Any] = json.loads(completed.stdout)
    assert report["image_uid"] == os.getuid()
    return {key: report[key] for key in smoke.SERVE_EXPECTED}


def _served(index: Path) -> dict[str, tuple[int, bytes]]:
    return {
        "/health": (200, b"{}"),
        "/": (200, index.read_bytes()),
        "/v1/sessions": (401, b"{}"),
    }


def test_serve_probe_waits_for_health_and_compares_the_index(tmp_path: Path) -> None:
    assert _probe(tmp_path, _served, unhealthy=3) == smoke.SERVE_EXPECTED


def test_serve_probe_reports_what_it_got(tmp_path: Path) -> None:
    def other(index: Path) -> dict[str, tuple[int, bytes]]:
        return {
            "/health": (200, b"{}"),
            "/": (200, b"<html>not the build</html>"),
            "/v1/sessions": (200, b"[]"),
        }

    report = _probe(tmp_path, other)
    assert report == {"health": 200, "root": 200, "root_is_index": False, "v1": 200}


# --- Image metadata ----------------------------------------------------------


def _image(**changes: Any) -> dict[str, Any]:
    image: dict[str, Any] = {
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "User": "neurostack",
            "Env": ["PATH=/usr/local/bin", "SPA_DIST_DIR=/app/spa"],
            "Labels": {"org.opencontainers.image.revision": "abc123"},
            "Healthcheck": {"Test": ["CMD-SHELL", HEALTHCHECK]},
            "Cmd": list(COMMAND),
        },
    }
    config = changes.pop("Config", {})
    image.update(changes)
    image["Config"].update(config)
    return image


@pytest.mark.parametrize(
    ("image", "problem"),
    [
        (_image(), None),
        (_image(Architecture="arm64"), "platform is linux/arm64"),
        (_image(Config={"Labels": {}}), "org.opencontainers.image.revision is None"),
        (
            _image(Config={"Labels": {"org.opencontainers.image.revision": "unknown"}}),
            "is 'unknown', not 'abc123'",
        ),
        (_image(Config={"Labels": None}), "image.revision is None"),
        (_image(Config={"Env": ["SPA_DIST_DIR=/srv"]}), "SPA_DIST_DIR is not /app/spa"),
        (_image(Config={"User": ""}), "user is ''"),
        (_image(Config={"Healthcheck": None}), "no HEALTHCHECK command"),
        (_image(Config={"Healthcheck": {"Test": ["NONE"]}}), "no HEALTHCHECK"),
        (_image(Config={"Cmd": None}), "no CMD"),
    ],
    ids=[
        "ok",
        "arch",
        "no-label",
        "default-label",
        "null-labels",
        "spa-env",
        "root",
        "no-healthcheck",
        "healthcheck-disabled",
        "no-cmd",
    ],
)
def test_image_metadata_problems(image: dict[str, Any], problem: str | None) -> None:
    problems = smoke.image_problems(image, "abc123")
    if problem is None:
        assert problems == []
    else:
        assert any(problem in item for item in problems), problems


def test_revision_constants_match_the_dockerfile() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert f"ARG {smoke.REVISION_ARG}=unknown\n" in text
    assert f'LABEL {smoke.REVISION_LABEL}="${{{smoke.REVISION_ARG}}}"\n' in text


def test_build_warnings_are_collected() -> None:
    log = "\n".join(
        [
            "#11 41.90 WARNING: Running pip as the 'root' user",
            " 1 warning found (use docker --debug to expand):",
            "#13 3.10 The lock file might not be compatible with the current",
            "#28 24.48 (!) Some chunks are larger than 500 kB after minification.",
            "#7 DONE 0.1s",
            "#28 7.0   - Downgrading packaging (26.3 -> 26.2)",
        ]
    )
    assert smoke.build_warnings(log) == [
        "#11 41.90 WARNING: Running pip as the 'root' user",
        "1 warning found (use docker --debug to expand):",
        "#13 3.10 The lock file might not be compatible with the current",
        "#28 24.48 (!) Some chunks are larger than 500 kB after minification.",
    ]


def test_main_refuses_without_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke, "docker_ready", lambda: False)
    assert smoke.main(["--expected-head", "035"]) == 2


def test_main_requires_the_expected_head() -> None:
    with pytest.raises(SystemExit):
        smoke.main([])


@pytest.mark.parametrize(("status", "code"), [("pass", 0), ("fail", 1)])
def test_main_exit_status_follows_the_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    code: int,
) -> None:
    seen: dict[str, Any] = {}

    def fake_smoke(*args: Any) -> smoke.Summary:
        seen["args"] = args
        return smoke.Summary(image="img:t", revision="abc", status=status)

    monkeypatch.setattr(smoke, "docker_ready", lambda: True)
    monkeypatch.setattr(smoke, "smoke", fake_smoke)
    monkeypatch.setattr(smoke, "tree_revision", lambda root: "abc-dirty")
    assert smoke.main(["--tag", "img:t", "--expected-head", "035"]) == code
    assert json.loads(capsys.readouterr().out)["status"] == status
    assert seen["args"][1:4] == ("img:t", "abc-dirty", "035")


class FakeDocker:
    """Answers the build, the inspect, every check and the serve container of
    ``smoke.smoke``; ``answers`` overrides one step (``start``, ``probe``,
    ``healthcheck``, ``logs``, ``rm``) with a result or an exception."""

    def __init__(
        self,
        image: dict[str, Any],
        build_code: int = 0,
        answers: dict[str, Any] | None = None,
    ) -> None:
        self.image = image
        self.build_code = build_code
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def _answer(
        self, step: str, default: subprocess.CompletedProcess[str]
    ) -> subprocess.CompletedProcess[str]:
        answer = self.answers.get(step, default)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, subprocess.CompletedProcess)
        return answer

    def __call__(
        self, argv: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "buildx", "build"]:
            return _completed("#1 DONE 0.1s\n", returncode=self.build_code)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _completed(json.dumps([self.image]))
        if argv[:3] == ["docker", "run", "--detach"]:
            return self._answer("start", _completed("c0ffee\n"))
        if argv[:2] == ["docker", "exec"] and smoke.SERVE_PROBE in argv:
            return self._answer("probe", _completed(json.dumps(SERVED)))
        if argv[:2] == ["docker", "exec"]:
            return self._answer("healthcheck", _completed())
        if argv[:2] == ["docker", "logs"]:
            return self._answer("logs", _completed("uvicorn log line\n"))
        if argv[:2] == ["docker", "rm"]:
            return self._answer("rm", _completed(argv[-1] + "\n"))
        return _completed("ok\n")


def _smoke_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker: FakeDocker
) -> smoke.Summary:
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    passing = smoke.Check("probe", ("true",), lambda completed: None)
    monkeypatch.setattr(smoke, "_run", docker)
    monkeypatch.setattr(smoke, "checks", lambda head, lock: [passing])
    return smoke.smoke(tmp_path, "img:t", "abc123", "035", None)


def test_smoke_passes_and_checks_the_image_id_not_the_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    docker = FakeDocker(_image(Id=IMAGE_ID, Config={}))
    summary = _smoke_with(monkeypatch, tmp_path, docker)
    assert summary.status == "pass", summary
    assert summary.image_id == IMAGE_ID
    runs = [call for call in docker.calls if call[:2] == ["docker", "run"]]
    assert runs and all(IMAGE_ID in call and "img:t" not in call for call in runs)
    assert all(("--pull", "never") in list(zip(call, call[1:])) for call in runs)


@pytest.mark.parametrize(
    "image",
    [
        _image(Id=IMAGE_ID, Architecture="arm64"),
        _image(Id=IMAGE_ID, Config={"Labels": {}}),
        _image(Id=IMAGE_ID, Config={"User": "root"}),
    ],
    ids=["arch", "label", "user"],
)
def test_smoke_fails_on_image_problems_even_when_checks_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, image: dict[str, Any]
) -> None:
    summary = _smoke_with(monkeypatch, tmp_path, FakeDocker(image))
    assert summary.image_problems
    assert [check["status"] for check in summary.checks] == ["pass", "pass"]
    assert summary.status == "fail"


@pytest.mark.parametrize(
    ("docker", "error"),
    [
        (FakeDocker(_image(Id=IMAGE_ID), build_code=1), "build failed"),
        (FakeDocker(_image(Id="img:t")), "no image ID"),
        (FakeDocker(_image()), "no image ID"),
    ],
    ids=["build-failed", "id-is-a-tag", "no-id"],
)
def test_smoke_fails_without_a_built_image_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker: FakeDocker, error: str
) -> None:
    summary = _smoke_with(monkeypatch, tmp_path, docker)
    assert summary.status == "fail"
    assert summary.error is not None and error in summary.error
    assert not [call for call in docker.calls if call[:2] == ["docker", "run"]]


def test_docker_ready_needs_the_cli_and_a_reachable_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert smoke.docker_ready() is False
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker")

    def daemon(code: int | None) -> Any:
        def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if code is None:
                raise subprocess.TimeoutExpired("docker info", 30)
            return _completed(returncode=code)

        return run

    for code, ready in ((1, False), (None, False), (0, True)):
        monkeypatch.setattr(subprocess, "run", daemon(code))
        assert smoke.docker_ready() is ready, code


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_revision_label_marks_a_modified_tree_dirty(tmp_path: Path) -> None:
    # The label is the only provenance of a locally built image: a build of
    # uncommitted changes must never carry a bare commit id.
    _git(tmp_path, "init", "-q")
    (tmp_path / "file.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "file.txt")
    _git(
        tmp_path,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@invalid",
        "commit",
        "-qm",
        "base",
    )
    head = _git(tmp_path, "rev-parse", "HEAD")
    assert smoke.tree_revision(tmp_path) == head
    (tmp_path / "file.txt").write_text("two\n", encoding="utf-8")
    assert smoke.tree_revision(tmp_path) == f"{head}-dirty"
    (tmp_path / "file.txt").write_text("one\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("untracked\n", encoding="utf-8")
    assert smoke.tree_revision(tmp_path) == f"{head}-dirty"


# --- The checks wired into the smoke ------------------------------------------


def _passing_outputs() -> dict[str, subprocess.CompletedProcess[str]]:
    return {
        "imports": _completed(),
        "site-packages": _completed(_report()),
        "no-build-toolchain": _completed(_toolchain()),
        "native-libraries": _completed(_native()),
        "no-world-writable": _completed(_modes()),
        "src-imports": _completed("22 top-level modules resolve\n"),
        "kms-signers": _completed(smoke.KMS_SIGNERS_OUTPUT + "\n"),
        "alembic-heads": _completed("035 (head)\n"),
        "spa-index": _completed(),
        "alembic-offline-sql": _completed(_sql(*WINDOW)),
    }


class CheckDocker(FakeDocker):
    """FakeDocker answering every real check of ``smoke.checks`` by the
    command it runs in the image (an unknown command fails the test)."""

    def __init__(
        self,
        outputs: dict[str, subprocess.CompletedProcess[str]] | None = None,
        answers: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(_image(Id=IMAGE_ID), answers=answers)
        self.names = {check.argv: check.name for check in smoke.checks("035", LOCK)}
        self.outputs = {**_passing_outputs(), **(outputs or {})}
        self.ran: list[str] = []

    def __call__(
        self, argv: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["docker", "run", "--rm"]:
            self.calls.append(list(argv))
            name = self.names[tuple(argv[argv.index(IMAGE_ID) + 1 :])]
            self.ran.append(name)
            return self.outputs[name]
        return super().__call__(argv, timeout)


def _full_smoke(monkeypatch: pytest.MonkeyPatch, docker: CheckDocker) -> smoke.Summary:
    # The real checks against the real poetry.lock: only Docker is faked.
    monkeypatch.setattr(smoke, "_run", docker)
    return smoke.smoke(ROOT, "img:t", "abc123", "035", None)


def _statuses(summary: smoke.Summary) -> dict[str, str]:
    return {check["name"]: check["status"] for check in summary.checks}


def test_full_smoke_passes_only_when_every_check_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = CheckDocker()
    summary = _full_smoke(monkeypatch, docker)
    assert summary.status == "pass", summary
    assert docker.ran == list(_checks())
    assert _statuses(summary) == {name: "pass" for name in [*_checks(), "serve"]}


@pytest.mark.parametrize(
    ("name", "output"),
    [
        ("no-build-toolchain", _completed()),
        ("native-libraries", _completed()),
        ("no-world-writable", _completed()),
        ("site-packages", _completed()),
        ("src-imports", _completed("boto3 (src/x.py)\n")),
        ("kms-signers", _completed("Boto3KmsDigestSigner\n")),
        ("alembic-heads", _completed("034 (head)\n")),
        ("alembic-offline-sql", _completed(_sql(*WINDOW[1:]))),
        ("imports", _completed(returncode=1, stderr="ModuleNotFoundError")),
        ("spa-index", _completed(returncode=1)),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_full_smoke_fails_on_any_exit_zero_failure(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    output: subprocess.CompletedProcess[str],
) -> None:
    summary = _full_smoke(monkeypatch, CheckDocker({name: output}))
    assert summary.status == "fail"
    statuses = _statuses(summary)
    assert statuses.pop(name) == "fail"
    assert set(statuses.values()) == {"pass"}


@pytest.mark.parametrize(
    ("name", "report"),
    [
        ("no-build-toolchain", lambda: _with_binary("/usr/bin", "gcc")),
        (
            "native-libraries",
            lambda: _native(unresolved={EXTENSION: ["libpq.so.5"]}),
        ),
        (
            "native-libraries",
            lambda: _native(unresolved={}, versions={EXTENSION: [TOO_OLD]}),
        ),
        ("no-world-writable", lambda: _modes(app=["/app/src/api/main.py"])),
    ],
    ids=["gcc-present", "library-missing", "library-too-old", "world-writable"],
)
def test_full_smoke_fails_on_a_new_check_s_finding(
    monkeypatch: pytest.MonkeyPatch, name: str, report: Callable[[], str]
) -> None:
    # Each report exits 0: only the check's own validator can fail it.
    summary = _full_smoke(monkeypatch, CheckDocker({name: _completed(report())}))
    assert summary.status == "fail"
    statuses = _statuses(summary)
    assert statuses.pop(name) == "fail"
    assert set(statuses.values()) == {"pass"}
    detail = next(c["detail"] for c in summary.checks if c["name"] == name)
    assert detail


def test_full_smoke_fails_when_only_serve_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Serving is the last check: a failure there must still fail the smoke
    # (the summary's image ID would otherwise be pushed).
    probe = _completed(json.dumps({**SERVED, "v1": 200}))
    summary = _full_smoke(monkeypatch, CheckDocker(answers={"probe": probe}))
    assert summary.status == "fail"
    statuses = _statuses(summary)
    assert statuses.pop("serve") == "fail"
    assert set(statuses.values()) == {"pass"}


# --- End to end (Docker host only) ------------------------------------------


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get(OPT_IN) != "1",
    reason=f"opt-in: set {OPT_IN}=1 to build the production image and run the "
    "smoke (needs Docker and the build network, takes minutes)",
)
def test_production_image_builds_and_passes_the_smoke(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if not smoke.docker_ready():
        pytest.fail(f"{OPT_IN}=1 but the Docker CLI or daemon is unavailable")
    status = smoke.main(
        [
            "--tag",
            "neurostack-api-smoke:pytest",
            "--expected-head",
            current_head(),
            "--build-log",
            str(tmp_path / "build.log"),
        ]
    )
    summary = json.loads(capsys.readouterr().out)
    assert status == 0, json.dumps(summary, indent=2)
    assert summary["status"] == "pass"
    assert [check["name"] for check in summary["checks"]] == [*_checks(), "serve"]
    assert summary["image_problems"] == []
