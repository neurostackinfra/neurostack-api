#!/usr/bin/env python3
"""Build the production image from the working tree and smoke it offline.

The build is ``docker buildx build --platform linux/amd64 --load --target
production`` of the repository root, with the source revision passed as the
``VCS_REF`` build argument. Nothing is pushed and no registry login happens.

Every check then runs inside the built image, addressed by its image ID (not
the tag) with ``--pull never`` and ``--network none``, as the image's own user
(``serve`` runs one container detached and probes it with ``docker exec``):
no database, AWS, ASAAS or registry is reachable, so a check that needs one
fails instead of reaching it, and the checked image is exactly the one whose ID
the summary reports. Push that image ID; never rebuild it for the push.

* ``imports``: ``python -c "import src.api.main, boto3, botocore"``.
* ``site-packages``: the interpreter's distributions are exactly the main group
  of ``poetry.lock`` for that interpreter (markers evaluated in the image), at
  the locked versions, plus the base image's pip, setuptools and wheel (pip and
  setuptools at their locked versions too): no Poetry, no Python build backend
  other than setuptools and wheel, and no distribution with two metadata
  directories.
* ``no-build-toolchain``: no compiler, linker, binutils, make, pkg-config or
  ``pg_config`` (any ``<triplet>-`` prefixed or versioned spelling, e.g.
  ``x86_64-linux-gnu-gcc-14``) in PATH or the system binary directories, no
  ``/usr/lib/gcc*`` or ``/usr/libexec/gcc*``, no file at all under the include
  directories outside the interpreter's own ``/usr/local/include/python3.11``
  (shipped by the base image; the C++ standard headers have no extension),
  nothing in ``/var/lib/apt/lists`` and no build package (``build-essential``,
  gcc, make, binutils, any ``-dev``) in the dpkg database.
* ``native-libraries``: ``ldd`` over every ELF file under ``/usr/local`` (the
  interpreter, libpython, lib-dynload, every site-packages extension and
  vendored library) resolves every library and every symbol version it needs
  (ldd's ``version `GLIBC_2.38' not found`` lines, which exit 0, fail it too),
  so none is missing or too old on a code path the other checks never load.
  The one exception is CPython's ``_tkinter`` missing Tk and Tcl (they are not
  in python:slim, and nothing imports tkinter).
* ``no-world-writable``: no file or directory under ``/app`` or site-packages
  is writable by any user. A build context on DrvFs (the ``/mnt/c`` checkout
  in WSL) shows every file as 0777 and COPY keeps it: build from a clone on
  the Linux filesystem.
* ``src-imports``: every absolute top-level import under ``src/``, lazy ones
  included, resolves in the image.
* ``kms-signers``: the KMS receipt and grant signer factories build their
  clients (they fail closed when the AWS SDK is missing). No KMS call is made.
* ``alembic-heads``: ``alembic heads`` prints exactly one line, the head
  named by ``--expected-head``: the head of the migrations the image contains
  (035 on main today, 036 once PR #121 is merged), not the production window
  below, which stays 032:035.
* ``spa-index``: ``test -f /app/spa/index.html`` (``SPA_DIST_DIR`` of the
  Dockerfile, read by ``src/api/spa.py``).
* ``alembic-offline-sql``: ``alembic upgrade 032:035 --sql`` emits the
  production migration window as SQL without a database. The window starts at
  032 because migration 017 reads rows (``fetchall``), which offline mode
  cannot do: ``alembic upgrade 035 --sql`` from an empty history fails there.
* ``serve``: the image runs its own CMD (uvicorn with its workers, as
  ``neurostack``) with ``ENVIRONMENT=staging``, a throwaway ``SECRET_KEY`` and
  no database, still under ``--network none``, plus ``--lifespan off``: the
  lifespan opens with a ``SELECT 1`` on ``DATABASE_URL`` and stops the app
  without one. Probed from inside the container over loopback, ``/health``
  answers 200, ``/`` serves ``/app/spa/index.html`` byte for byte,
  ``/v1/sessions`` answers 401 and PID 1 runs as ``neurostack``; then the
  image's own HEALTHCHECK command must exit 0. The container is force-removed
  whatever the outcome.

The image metadata must say linux/amd64, carry the revision label and
``SPA_DIST_DIR=/app/spa``, run as ``neurostack`` and declare a HEALTHCHECK. A
JSON summary (timings, image size, build warnings, per-check status) goes to
stdout. Exit status: 0 when the build and every check pass, 1 otherwise, 2
when Docker is unusable.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.markers import Marker
from packaging.utils import canonicalize_name

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "linux/amd64"
TARGET = "production"
DEFAULT_TAG = "neurostack-api-smoke:local"
REVISION_ARG = "VCS_REF"
REVISION_LABEL = "org.opencontainers.image.revision"
SPA_DIST_DIR = "/app/spa"
IMAGE_USER = "neurostack"
# The production migration window (docs/runbooks/asaas-deployment.md, gate 5):
# alembic_version reads 032 before the run, which applies exactly 035.
WINDOW_FROM = "032"
WINDOW_TO = "035"
IMPORTS = "import src.api.main, boto3, botocore"
KMS_SIGNERS = (
    "from src.api.routes.billing_producer import _receipt_signer; "
    "from src.api.routes.billing_producer_setup import _grant_signer; "
    "print(type(_receipt_signer()).__name__, type(_grant_signer()).__name__)"
)
KMS_SIGNERS_OUTPUT = "Boto3KmsDigestSigner Boto3KmsDigestSigner"
# Every absolute top-level import anywhere under src/ (also the lazy ones, like
# the signers' boto3) must resolve in the image.
SRC_IMPORTS = """
import ast, importlib.util, pathlib, sys
first_party = set(sys.stdlib_module_names) | {"src", "neurostack_core", "__future__"}
found = {}
for path in sorted(pathlib.Path("src").rglob("*.py")):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            found.setdefault(name.partition(".")[0], str(path))
missing = sorted(
    f"{name} ({path})"
    for name, path in found.items()
    if name not in first_party and importlib.util.find_spec(name) is None
)
if not found:
    sys.exit("no import found under src/")
print("\\n".join(missing) or f"{len(found)} top-level modules resolve")
sys.exit(1 if missing else 0)
"""
SRC_IMPORTS_OUTPUT = re.compile(r"^[1-9]\d* top-level modules resolve$")
# What the base image's interpreter ships besides the lock's main group (the
# Dockerfile's base and production stages move pip and setuptools to their
# locked versions; wheel stays the pinned image's own).
BASE_DISTRIBUTIONS = frozenset({"pip", "setuptools", "wheel"})
# The interpreter's PEP 508 marker environment and every distribution it sees.
SITE_PACKAGES = """
import importlib.metadata, json, os, platform, sys
environment = {
    "implementation_name": sys.implementation.name,
    "implementation_version": ".".join(map(str, sys.implementation.version[:3])),
    "os_name": os.name,
    "platform_machine": platform.machine(),
    "platform_python_implementation": platform.python_implementation(),
    "platform_release": platform.release(),
    "platform_system": platform.system(),
    "platform_version": platform.version(),
    "python_full_version": platform.python_version(),
    "python_version": ".".join(platform.python_version_tuple()[:2]),
    "sys_platform": sys.platform,
}
distributions = sorted(
    [str(item.metadata["Name"]), item.version, str(item.locate_file(""))]
    for item in importlib.metadata.distributions()
)
print(json.dumps({"environment": environment, "distributions": distributions}))
"""
# Every binary directory (PATH plus the system ones), the compiler support
# directories, every file under the include directories (whatever its name:
# the C++ standard headers have none), the apt lists and the installed dpkg
# packages of the filesystem at argv[1] (the smoke passes /, the unit tests a
# fake root).
TOOLCHAIN = """
import glob, json, os, sys
root = os.path.realpath(sys.argv[1])
def at(path):
    return os.path.join(root, path.lstrip("/"))
def shown(path):
    relative = os.path.relpath(path, root)
    return "/" if relative == "." else "/" + relative
directories = os.environ.get("PATH", "").split(os.pathsep)
directories += ["/usr/bin", "/usr/local/bin", "/usr/sbin", "/usr/local/sbin", "/bin", "/sbin"]
binaries = {}
for directory in filter(None, directories):
    real = os.path.realpath(at(directory))
    if os.path.isdir(real) and shown(real) not in binaries:
        binaries[shown(real)] = sorted(os.listdir(real))
compiler_dirs = sorted(
    shown(path)
    for pattern in ("/usr/lib/gcc*", "/usr/libexec/gcc*", "/usr/local/lib/gcc*")
    for path in glob.glob(at(pattern))
)
headers = sorted(
    shown(os.path.join(parent, name))
    for top in ("/usr/include", "/usr/local/include")
    for parent, _, names in os.walk(at(top))
    for name in names
)
lists = at("/var/lib/apt/lists")
apt_lists = sorted(os.listdir(lists)) if os.path.isdir(lists) else []
packages = []
with open(at("/var/lib/dpkg/status"), encoding="utf-8") as status:
    for block in status.read().split("\\n\\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if ": " in line and not line[:1].isspace()
        )
        if fields.get("Status") == "install ok installed":
            packages.append(fields["Package"])
print(json.dumps({
    "binaries": binaries,
    "compiler_dirs": compiler_dirs,
    "headers": headers,
    "apt_lists": apt_lists,
    "packages": sorted(packages),
}))
"""
# Compiler, assembler, linker, binutils, make, pkg-config and pg_config names,
# bare or with Debian's <triplet>- prefix and -<version> suffix
# (x86_64-linux-gnu-gcc-14, aarch64-linux-gnu-ld.bfd, g++-14, cpp-14).
_BUILD_TOOL = re.compile(
    r"^(?:[a-z0-9_]+-linux-gnu-)?"
    r"(?:gcc|g\+\+|cc|c\+\+|c89|c99|c89-gcc|c99-gcc|cpp|gcc-ar|gcc-nm|gcc-ranlib"
    r"|lto-dump|clang|clang\+\+|ld|ld\.bfd|ld\.gold|as|ar|ranlib|nm|objcopy"
    r"|objdump|strip|readelf|make|gmake|pkg-config|pkgconf|pg_config)"
    r"(?:-\d+(?:\.\d+)*)?$"
)
# build-essential and what it pulls in, and every -dev package (libpq-dev,
# libc6-dev, libssl-dev...). gcc-14-base (documentation only, shipped by the
# base image) is not one.
_BUILD_PACKAGE = re.compile(
    r"^(?:build-essential|make|binutils(?:-[a-z0-9-]+)?|libbinutils|pkgconf(?:-bin)?"
    r"|pkg-config|dpkg-dev|libc-dev-bin|clang(?:-\d+)?"
    r"|(?:gcc|g\+\+|cpp)(?:-\d+)?(?:-[a-z0-9-]+-linux-gnu)?|.+-dev)$"
)
# CPython's C API headers, shipped by the pinned base image: harmless without a
# compiler, and deleting them in a later layer would not shrink the image.
INTERPRETER_HEADERS = "/usr/local/include/python3.11/"
# ldd over every ELF file under argv[1] (the smoke passes /usr/local: the
# interpreter, libpython, lib-dynload and every site-packages extension or
# vendored library): which ones were scanned, which libraries do not resolve,
# which need a symbol version their library lacks (a library present but too
# old: ldd prints "<file>: <library>: version `GLIBC_2.38' not found (required
# by <file>)" and still exits 0), and where ldd itself failed. A static ELF is
# not an error.
NATIVE_LIBRARIES = """
import json, os, re, subprocess, sys
elf, unresolved, versions, errors = [], {}, {}, {}
for parent, _, names in os.walk(sys.argv[1]):
    for name in names:
        path = os.path.join(parent, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            if handle.read(4) != b"\\x7fELF":
                continue
        elf.append(path)
        run = subprocess.run(["ldd", path], capture_output=True, text=True)
        output = run.stdout + "\\n" + run.stderr
        missing = re.findall(r"^\\s*(\\S+) => not found$", run.stdout, re.MULTILINE)
        too_old = re.findall(r"^.*\\bversion `[^`'\\n]+' not found\\b.*$", output, re.MULTILINE)
        if missing:
            unresolved[path] = sorted(missing)
        if too_old:
            versions[path] = sorted({line.strip() for line in too_old})
        if run.returncode and not missing and "not a dynamic executable" not in output:
            errors[path] = (run.stderr or run.stdout).strip()[-200:]
print(json.dumps({
    "elf": sorted(elf),
    "unresolved": unresolved,
    "versions": versions,
    "errors": errors,
}))
"""
NATIVE_ROOT = "/usr/local"
INTERPRETER = "/usr/local/bin/python3.11"
SITE_PACKAGES_DIR = "/usr/local/lib/python3.11/site-packages"
# The only allowed gap: CPython's Tk binding. python:slim ships _tkinter
# without Tk/Tcl, no package or module in the image imports tkinter
# (tests/scripts/test_production_image_smoke.py scans src/ and the core
# package), and the module is never loaded.
_TKINTER = re.compile(
    r"^/usr/local/lib/python3\.11/lib-dynload/"
    r"_tkinter\.cpython-311-[a-z0-9_]+-linux-gnu\.so$"
)
_TKINTER_LIBRARIES = frozenset({"libtk8.6.so", "libtcl8.6.so"})
# Every path (file or directory, symlinks aside) under argv[1:], and the ones
# any user may write. A build context on DrvFs (the /mnt/c checkout in WSL)
# reports every file as 0777 and COPY keeps that mode.
WORLD_WRITABLE = """
import json, os, stat, sys
report = {}
for top in sys.argv[1:]:
    scanned, writable = 0, []
    paths = [top] if os.path.isdir(top) else []
    for parent, dirs, names in os.walk(top):
        paths += [os.path.join(parent, name) for name in dirs + names]
    for path in paths:
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            continue
        scanned += 1
        if mode & stat.S_IWOTH:
            writable.append(path)
    report[top] = {"scanned": scanned, "writable": sorted(writable)}
print(json.dumps(report))
"""
WRITABLE_ROOTS = ("/app", SITE_PACKAGES_DIR)
# The serve check runs the image's own CMD, offline and with no database. The
# application lifespan opens with init_database (SELECT 1 on DATABASE_URL) and
# exits without one, so the CMD gets ``--lifespan off``: the same uvicorn,
# workers, uvloop and httptools, app import, middleware, auth and SPA, minus
# the startup hooks that need PostgreSQL and Redis.
SERVE_ENVIRONMENT = "staging"
SERVE_LIFESPAN = ("--lifespan", "off")
SERVE_DEADLINE_SECONDS = 90
SERVE_EXPECTED = {"health": 200, "root": 200, "root_is_index": True, "v1": 401}
SERVE_URL = "http://127.0.0.1:8000"
# Run inside the serving container (loopback only) with argv: base URL, index
# file, image user, deadline. Wait for /health, then fetch the SPA root and an
# API path without credentials; report the statuses, whether / is the index
# file byte for byte, and who runs PID 1.
SERVE_PROBE = """
import json, os, pwd, sys, time, urllib.error, urllib.request
base, index_file, user, seconds = sys.argv[1:5]
def fetch(path):
    try:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except OSError:
        return None, b""
deadline = time.monotonic() + float(seconds)
health, _ = fetch("/health")
while health != 200 and time.monotonic() < deadline:
    time.sleep(0.5)
    health, _ = fetch("/health")
root, body = fetch("/")
v1, _ = fetch("/v1/sessions")
try:
    with open(index_file, "rb") as index:
        root_is_index = bool(body) and body == index.read()
except OSError:
    root_is_index = False
print(json.dumps({
    "health": health,
    "root": root,
    "root_is_index": root_is_index,
    "v1": v1,
    "pid1_uid": os.stat("/proc/1").st_uid,
    "image_uid": pwd.getpwnam(user).pw_uid,
}))
"""
# No credentials, no shared config and no instance metadata: building a client
# reads none of them, and nothing may be signed from inside a smoke check.
OFFLINE_AWS_ENV = (
    ("AWS_EC2_METADATA_DISABLED", "true"),
    ("AWS_CONFIG_FILE", "/nonexistent"),
    ("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent"),
)
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
BUILD_TIMEOUT_SECONDS = 3600
CHECK_TIMEOUT_SECONDS = 300
# BuildKit and pip warnings, Poetry's lock compatibility notice and Vite's (!).
_WARNING = re.compile(
    r"\bWARN(?:ING)?\b|\bwarnings? found\b|might not be compatible|\(!\)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    validate: Callable[[subprocess.CompletedProcess[str]], str | None]
    env: tuple[tuple[str, str], ...] = ()


@dataclass
class Summary:
    image: str
    revision: str
    platform: str = PLATFORM
    status: str = "fail"
    build_seconds: float | None = None
    image_id: str | None = None
    size_bytes: int | None = None
    build_warnings: list[str] = field(default_factory=list)
    image_problems: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def _exit_zero(completed: subprocess.CompletedProcess[str]) -> str | None:
    if completed.returncode != 0:
        return f"exit status {completed.returncode}: {_tail(completed)}"
    return None


def _tail(completed: subprocess.CompletedProcess[str], lines: int = 20) -> str:
    output = (completed.stderr or "") + (completed.stdout or "")
    return "\n".join(output.strip().splitlines()[-lines:])


def heads_problem(stdout: str, expected_head: str) -> str | None:
    """``alembic heads`` must print exactly ``<head> (head)`` and nothing else."""

    lines = [line.strip() for line in stdout.strip().splitlines()]
    if lines != [f"{expected_head} (head)"]:
        return f"expected exactly ['{expected_head} (head)'], got {lines}"
    return None


def offline_sql_problem(stdout: str, window_from: str, window_to: str) -> str | None:
    """One transaction that walks the window revision by revision to its end."""

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if lines.count("BEGIN;") != 1 or lines[0] != "BEGIN;" or lines[-1] != "COMMIT;":
        return "emitted SQL is not exactly one BEGIN; ... COMMIT; transaction"
    steps = re.findall(r"^-- Running upgrade (\S+) -> (\S+)$", stdout, re.MULTILINE)
    if not steps:
        return "no '-- Running upgrade' step in the emitted SQL"
    if steps[0][0] != window_from or steps[-1][1] != window_to:
        return f"steps {steps} do not run from {window_from} to {window_to}"
    if any(prev[1] != step[0] for prev, step in zip(steps, steps[1:])):
        return f"steps {steps} are not contiguous"
    final = (
        f"UPDATE alembic_version SET version_num='{window_to}' "
        f"WHERE alembic_version.version_num = '{steps[-1][0]}';"
    )
    if lines[-2] != final:
        return f"the transaction does not end with {final!r}"
    return None


def locked_main(
    lock: Mapping[str, Any], environment: Mapping[str, str]
) -> dict[str, str]:
    """Name and version of every main-group package the lock installs here."""

    locked: dict[str, str] = {}
    for package in lock.get("package", []):
        if "main" not in package.get("groups", []) or package.get("optional"):
            continue
        markers = package.get("markers")
        marker = markers.get("main") if isinstance(markers, dict) else markers
        if marker and not Marker(marker).evaluate(dict(environment)):
            continue
        locked[canonicalize_name(package["name"])] = package["version"]
    return locked


def site_packages_problem(stdout: str, lock: Mapping[str, Any]) -> str | None:
    try:
        report = json.loads(stdout)
        expected = locked_main(lock, report["environment"])
        listed = [(canonicalize_name(n), v) for n, v, _ in report["distributions"]]
    except (ValueError, KeyError, TypeError) as error:
        return f"unreadable distribution report: {error!r}"
    if not expected:
        return "poetry.lock has no main-group package for this interpreter"
    found: dict[str, list[str]] = {}
    for name, version in listed:
        found.setdefault(name, []).append(version)
    problems = [
        f"{name} has {len(versions)} metadata directories {sorted(versions)}"
        for name, versions in sorted(found.items())
        if len(versions) > 1
    ]
    problems += [
        f"{name} {expected[name]} is missing" for name in expected if name not in found
    ]
    problems += [
        f"{name} is {found[name]}, locked {expected[name]}"
        for name in expected
        if name in found and found[name] != [expected[name]]
    ]
    tooling = locked_tooling(lock)
    problems += [
        f"{name} is {found[name]}, locked {tooling[name]}"
        for name in sorted(set(tooling) - set(expected))
        if name in found and found[name] != [tooling[name]]
    ]
    problems += [
        f"{name} {found[name]} is not in the lock's main group"
        for name in sorted(set(found) - set(expected) - BASE_DISTRIBUTIONS)
    ]
    return "; ".join(problems) or None


def locked_tooling(lock: Mapping[str, Any]) -> dict[str, str]:
    """The lock's version, in any group, of each base distribution it pins
    (pip and setuptools; the base image's wheel is not in the lock)."""

    return {
        canonicalize_name(package["name"]): package["version"]
        for package in lock.get("package", [])
        if canonicalize_name(package["name"]) in BASE_DISTRIBUTIONS
    }


def is_build_package(name: str) -> bool:
    """A Debian package that only builds software (compilers, -dev headers)."""

    return _BUILD_PACKAGE.match(name) is not None


def toolchain_problem(stdout: str) -> str | None:
    try:
        report = json.loads(stdout)
        binaries = {
            str(directory): [str(name) for name in names]
            for directory, names in report["binaries"].items()
        }
        compiler_dirs = [str(path) for path in report["compiler_dirs"]]
        headers = [str(path) for path in report["headers"]]
        apt_lists = [str(name) for name in report["apt_lists"]]
        packages = [str(name) for name in report["packages"]]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return f"unreadable toolchain report: {error!r}"
    # Nothing scanned or no package database read would pass vacuously.
    if not binaries.get("/usr/bin") or "python3" not in binaries.get(
        "/usr/local/bin", []
    ):
        return "the report lists no /usr/bin or no /usr/local/bin/python3"
    if not {"libc6", "dpkg"} <= set(packages):
        return "the report lists no libc6 or dpkg package"
    problems: list[str] = []
    tools = sorted(
        f"{directory}/{name}"
        for directory, names in binaries.items()
        for name in names
        if _BUILD_TOOL.match(name)
    )
    if tools:
        problems.append(f"{len(tools)} build tools: {tools[:10]}")
    if compiler_dirs:
        problems.append(f"compiler directories: {compiler_dirs}")
    stray = [path for path in headers if not path.startswith(INTERPRETER_HEADERS)]
    if stray:
        problems.append(
            f"{len(stray)} headers outside {INTERPRETER_HEADERS}: {stray[:10]}"
        )
    if apt_lists:
        problems.append(f"/var/lib/apt/lists holds {apt_lists}")
    build = [name for name in packages if is_build_package(name)]
    if build:
        problems.append(f"{len(build)} build packages installed: {build[:10]}")
    return "; ".join(problems) or None


def native_libraries_problem(stdout: str) -> str | None:
    try:
        report = json.loads(stdout)
        elf = [str(path) for path in report["elf"]]
        unresolved = {
            str(path): [str(name) for name in names]
            for path, names in report["unresolved"].items()
        }
        versions = {
            str(path): [str(line) for line in lines]
            for path, lines in report["versions"].items()
        }
        errors = {str(path): str(error) for path, error in report["errors"].items()}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return f"unreadable native library report: {error!r}"
    # A scan that found neither the interpreter nor an extension module proves
    # nothing.
    if INTERPRETER not in elf or not any(
        path.startswith(SITE_PACKAGES_DIR + "/") for path in elf
    ):
        return f"the report lists no {INTERPRETER} or no site-packages ELF file"
    gaps = {
        path: names
        for path, names in sorted(unresolved.items())
        if not (_TKINTER.match(path) and set(names) <= _TKINTER_LIBRARIES)
    }
    problems: list[str] = []
    if gaps:
        sample = dict(list(gaps.items())[:10])
        problems.append(f"{len(gaps)} ELF files with unresolved libraries: {sample}")
    # No exception here, _tkinter included: a library that loads but lacks a
    # symbol version fails at import time.
    if versions:
        sample = dict(list(sorted(versions.items()))[:10])
        problems.append(
            f"{len(versions)} ELF files need a symbol version their library"
            f" lacks: {sample}"
        )
    if errors:
        problems.append(
            f"ldd failed on {len(errors)} files: {dict(list(errors.items())[:5])}"
        )
    return "; ".join(problems) or None


def world_writable_problem(stdout: str) -> str | None:
    try:
        report = json.loads(stdout)
        scanned = {top: int(report[top]["scanned"]) for top in WRITABLE_ROOTS}
        writable = {
            top: [str(path) for path in report[top]["writable"]]
            for top in WRITABLE_ROOTS
        }
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return f"unreadable mode report: {error!r}"
    problems = [
        f"nothing scanned under {top}" for top in WRITABLE_ROOTS if not scanned[top]
    ]
    problems += [
        f"{len(paths)} world-writable paths under {top} (a build context on a"
        f" DrvFs checkout such as /mnt/c? build from a Linux-filesystem clone):"
        f" {paths[:10]}"
        for top, paths in writable.items()
        if paths
    ]
    return "; ".join(problems) or None


def serve_problem(stdout: str) -> str | None:
    try:
        report = json.loads(stdout)
        found = {key: report.get(key) for key in SERVE_EXPECTED}
        uid, image_uid = report["pid1_uid"], report["image_uid"]
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return f"unreadable serve report: {error!r}"
    # Exact types: JSON true must not pass as 1, nor 200 as true.
    problems = [
        f"{key} is {found[key]!r}, expected {value!r}"
        for key, value in SERVE_EXPECTED.items()
        if type(found[key]) is not type(value) or found[key] != value
    ]
    if type(uid) is not int or uid == 0 or uid != image_uid:
        problems.append(f"PID 1 runs as uid {uid!r}, not {IMAGE_USER} ({image_uid!r})")
    return "; ".join(problems) or None


def checks(expected_head: str, lock: Mapping[str, Any]) -> list[Check]:
    def heads(completed: subprocess.CompletedProcess[str]) -> str | None:
        return _exit_zero(completed) or heads_problem(completed.stdout, expected_head)

    def offline_sql(completed: subprocess.CompletedProcess[str]) -> str | None:
        return _exit_zero(completed) or offline_sql_problem(
            completed.stdout, WINDOW_FROM, WINDOW_TO
        )

    def signers(completed: subprocess.CompletedProcess[str]) -> str | None:
        problem = _exit_zero(completed)
        if problem is None and completed.stdout.strip() != KMS_SIGNERS_OUTPUT:
            problem = f"unexpected signer types: {completed.stdout.strip()!r}"
        return problem

    def site_packages(completed: subprocess.CompletedProcess[str]) -> str | None:
        return _exit_zero(completed) or site_packages_problem(completed.stdout, lock)

    def src_imports(completed: subprocess.CompletedProcess[str]) -> str | None:
        problem = _exit_zero(completed)
        if problem is None and not SRC_IMPORTS_OUTPUT.match(completed.stdout.strip()):
            problem = f"unexpected output: {completed.stdout.strip()!r}"
        return problem

    def toolchain(completed: subprocess.CompletedProcess[str]) -> str | None:
        return _exit_zero(completed) or toolchain_problem(completed.stdout)

    def native(completed: subprocess.CompletedProcess[str]) -> str | None:
        return _exit_zero(completed) or native_libraries_problem(completed.stdout)

    def modes(completed: subprocess.CompletedProcess[str]) -> str | None:
        return _exit_zero(completed) or world_writable_problem(completed.stdout)

    return [
        Check("imports", ("python", "-c", IMPORTS), _exit_zero),
        Check("site-packages", ("python", "-c", SITE_PACKAGES), site_packages),
        Check("no-build-toolchain", ("python", "-c", TOOLCHAIN, "/"), toolchain),
        Check(
            "native-libraries",
            ("python", "-c", NATIVE_LIBRARIES, NATIVE_ROOT),
            native,
        ),
        Check(
            "no-world-writable",
            ("python", "-c", WORLD_WRITABLE, *WRITABLE_ROOTS),
            modes,
        ),
        Check("src-imports", ("python", "-c", SRC_IMPORTS), src_imports),
        Check("kms-signers", ("python", "-c", KMS_SIGNERS), signers, OFFLINE_AWS_ENV),
        Check("alembic-heads", ("alembic", "heads"), heads),
        Check("spa-index", ("test", "-f", f"{SPA_DIST_DIR}/index.html"), _exit_zero),
        Check(
            "alembic-offline-sql",
            ("alembic", "upgrade", f"{WINDOW_FROM}:{WINDOW_TO}", "--sql"),
            offline_sql,
        ),
    ]


def build_argv(root: Path, tag: str, revision: str) -> list[str]:
    return [
        "docker",
        "buildx",
        "build",
        "--platform",
        PLATFORM,
        "--load",
        "--target",
        TARGET,
        "--build-arg",
        f"{REVISION_ARG}={revision}",
        "--tag",
        tag,
        "--progress",
        "plain",
        str(root),
    ]


def run_argv(image: str, check: Check) -> list[str]:
    argv = ["docker", "run", "--rm", "--pull", "never", "--network", "none"]
    argv += ["--platform", PLATFORM]
    for name, value in check.env:
        argv.extend(["--env", f"{name}={value}"])
    return [*argv, image, *check.argv]


def serve_argv(
    image: str, name: str, secret_key: str, command: Sequence[str]
) -> list[str]:
    """Run the image's CMD detached, offline, as the image's user."""

    argv = ["docker", "run", "--detach", "--name", name, "--pull", "never"]
    argv += ["--network", "none", "--platform", PLATFORM]
    argv += ["--env", f"ENVIRONMENT={SERVE_ENVIRONMENT}"]
    argv += ["--env", f"SECRET_KEY={secret_key}"]
    return [*argv, image, *command, *SERVE_LIFESPAN]


def command_argv(image: Mapping[str, Any]) -> list[str] | None:
    """The image's CMD (exec form), or None."""

    command = (image.get("Config") or {}).get("Cmd") or []
    if not isinstance(command, list) or not command:
        return None
    return [str(item) for item in command]


def healthcheck_argv(image: Mapping[str, Any]) -> list[str] | None:
    """The image's HEALTHCHECK as an argv to exec in its container."""

    test = ((image.get("Config") or {}).get("Healthcheck") or {}).get("Test") or []
    if len(test) == 2 and test[0] == "CMD-SHELL":
        return ["/bin/sh", "-c", str(test[1])]
    if len(test) > 1 and test[0] == "CMD":
        return [str(item) for item in test[1:]]
    return None


def _serve(
    image: str,
    name: str,
    command: Sequence[str] | None,
    healthcheck: Sequence[str] | None,
) -> str | None:
    if command is None:
        return "the image declares no CMD"
    if healthcheck is None:
        return "the image declares no HEALTHCHECK command"
    # A throwaway signing key: ENVIRONMENT=staging refuses the default one.
    secret_key = secrets.token_urlsafe(48)
    started = _run(serve_argv(image, name, secret_key, command), CHECK_TIMEOUT_SECONDS)
    if started.returncode != 0:
        return f"the container did not start: {_tail(started)}"
    probe = _run(
        [
            "docker",
            "exec",
            name,
            "python",
            "-c",
            SERVE_PROBE,
            SERVE_URL,
            f"{SPA_DIST_DIR}/index.html",
            IMAGE_USER,
            str(SERVE_DEADLINE_SECONDS),
        ],
        CHECK_TIMEOUT_SECONDS,
    )
    problem = _exit_zero(probe) or serve_problem(probe.stdout)
    if problem is None:
        health = _run(["docker", "exec", name, *healthcheck], CHECK_TIMEOUT_SECONDS)
        if health.returncode != 0:
            problem = f"HEALTHCHECK exited {health.returncode}: {_tail(health)}"
    if problem is not None:
        logs = _run(["docker", "logs", "--tail", "40", name], CHECK_TIMEOUT_SECONDS)
        problem += f"\ncontainer log:\n{_tail(logs, 40)}"
    return problem


def serve_check(
    image: str,
    command: Sequence[str] | None,
    healthcheck: Sequence[str] | None,
) -> dict[str, Any]:
    """Serve from the image for a few seconds; the container never outlives it."""

    started = time.monotonic()
    name = f"neurostack-smoke-serve-{secrets.token_hex(6)}"
    problem: str | None
    try:
        problem = _serve(image, name, command, healthcheck)
    except subprocess.TimeoutExpired as error:
        problem = f"timed out: {error.cmd[:3]}"
    finally:
        try:
            removed = _run(["docker", "rm", "--force", name], CHECK_TIMEOUT_SECONDS)
            leftover = removed.returncode != 0 and "No such container" not in (
                removed.stderr or ""
            )
            removal = f"container {name} not removed: {_tail(removed)}"
        except subprocess.TimeoutExpired:
            leftover, removal = True, f"removing container {name} timed out"
    if leftover:
        problem = f"{problem}; {removal}" if problem else removal
    return {
        "name": "serve",
        "status": "fail" if problem else "pass",
        "seconds": round(time.monotonic() - started, 1),
        "detail": problem,
    }


def image_problems(image: dict[str, Any], revision: str) -> list[str]:
    config = image.get("Config") or {}
    problems: list[str] = []
    platform = f"{image.get('Os')}/{image.get('Architecture')}"
    if platform != PLATFORM:
        problems.append(f"platform is {platform}, not {PLATFORM}")
    label = (config.get("Labels") or {}).get(REVISION_LABEL)
    if label != revision:
        problems.append(f"{REVISION_LABEL} is {label!r}, not {revision!r}")
    if f"SPA_DIST_DIR={SPA_DIST_DIR}" not in (config.get("Env") or []):
        problems.append(f"SPA_DIST_DIR is not {SPA_DIST_DIR}")
    if config.get("User") != IMAGE_USER:
        problems.append(f"user is {config.get('User')!r}, not {IMAGE_USER!r}")
    if healthcheck_argv(image) is None:
        problems.append("no HEALTHCHECK command")
    if command_argv(image) is None:
        problems.append("no CMD")
    return problems


def build_warnings(log: str) -> list[str]:
    return [line.strip() for line in log.splitlines() if _WARNING.search(line)]


def tree_revision(root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    revision = git("rev-parse", "HEAD")
    return f"{revision}-dirty" if git("status", "--porcelain") else revision


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        completed = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _run(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )


def smoke(
    root: Path,
    tag: str,
    revision: str,
    expected_head: str,
    build_log: Path | None,
) -> Summary:
    summary = Summary(image=tag, revision=revision)
    started = time.monotonic()
    built = _run(build_argv(root, tag, revision), BUILD_TIMEOUT_SECONDS)
    summary.build_seconds = round(time.monotonic() - started, 1)
    log = (built.stdout or "") + (built.stderr or "")
    if build_log is not None:
        build_log.write_text(log, encoding="utf-8")
    summary.build_warnings = build_warnings(log)
    if built.returncode != 0:
        summary.error = f"build failed ({built.returncode}): {_tail(built, 40)}"
        return summary

    inspected = _run(["docker", "image", "inspect", tag], CHECK_TIMEOUT_SECONDS)
    if inspected.returncode != 0:
        summary.error = f"image inspect failed: {_tail(inspected)}"
        return summary
    image = json.loads(inspected.stdout)[0]
    summary.image_id = image.get("Id")
    summary.size_bytes = image.get("Size")
    summary.image_problems = image_problems(image, revision)
    if not isinstance(summary.image_id, str) or not IMAGE_ID.match(summary.image_id):
        summary.error = f"image inspect returned no image ID: {summary.image_id!r}"
        return summary

    lock = tomllib.loads((root / "poetry.lock").read_text(encoding="utf-8"))
    for check in checks(expected_head, lock):
        started = time.monotonic()
        try:
            completed = _run(run_argv(summary.image_id, check), CHECK_TIMEOUT_SECONDS)
            problem = check.validate(completed)
        except subprocess.TimeoutExpired:
            problem = f"timed out after {CHECK_TIMEOUT_SECONDS}s"
        summary.checks.append(
            {
                "name": check.name,
                "status": "fail" if problem else "pass",
                "seconds": round(time.monotonic() - started, 1),
                "detail": problem,
            }
        )
    summary.checks.append(
        serve_check(summary.image_id, command_argv(image), healthcheck_argv(image))
    )
    passed = all(item["status"] == "pass" for item in summary.checks)
    summary.status = "pass" if passed and not summary.image_problems else "fail"
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument(
        "--revision", help="label value (default: git HEAD, '-dirty' when modified)"
    )
    parser.add_argument(
        "--expected-head",
        required=True,
        help="the single Alembic head of the migrations the image contains"
        " (e.g. 035); the production window stays 032:035 whatever it is",
    )
    parser.add_argument("--build-log", type=Path, help="write the build output here")
    args = parser.parse_args(argv)

    if not docker_ready():
        print("docker CLI or daemon unavailable", file=sys.stderr)
        return 2
    root = args.root.resolve()
    summary = smoke(
        root,
        args.tag,
        args.revision or tree_revision(root),
        args.expected_head,
        args.build_log,
    )
    print(json.dumps(summary.__dict__, indent=2))
    return 0 if summary.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
