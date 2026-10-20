#!/usr/bin/env python3
"""Build the production image from the working tree and smoke it offline.

The build is ``docker buildx build --platform linux/amd64 --load --target
production`` of the repository root, with the source revision passed as the
``VCS_REF`` build argument. Nothing is pushed and no registry login happens.

Every check then runs inside the built image, addressed by its image ID (not
the tag) with ``--pull never`` and ``--network none``, as the image's own user:
no database, AWS, ASAAS or registry is reachable, so a check that needs one
fails instead of reaching it, and the checked image is exactly the one whose ID
the summary reports. Push that image ID; never rebuild it for the push.

* ``imports``: ``python -c "import src.api.main, boto3, botocore"``.
* ``site-packages``: the interpreter's distributions are exactly the main group
  of ``poetry.lock`` for that interpreter (markers evaluated in the image), at
  the locked versions, plus the base image's pip, setuptools and wheel: no
  Poetry, no Python build backend other than setuptools and wheel, and no
  distribution with two metadata directories. Only Python distributions are
  checked: the system compilers and headers of ``build-essential`` and
  ``libpq-dev`` (gcc, g++, make, ld, as, cpp, pg_config) are still in the image,
  because the production stage builds on ``base``.
* ``src-imports``: every absolute top-level import under ``src/``, lazy ones
  included, resolves in the image.
* ``kms-signers``: the KMS receipt and grant signer factories build their
  clients (they fail closed when the AWS SDK is missing). No KMS call is made.
* ``alembic-heads``: ``alembic heads`` prints exactly one line, the head
  named by ``--expected-head`` (035 while 036 and 037 are not on main).
* ``spa-index``: ``test -f /app/spa/index.html`` (``SPA_DIST_DIR`` of the
  Dockerfile, read by ``src/api/spa.py``).
* ``alembic-offline-sql``: ``alembic upgrade 032:035 --sql`` emits the
  production migration window as SQL without a database. The window starts at
  032 because migration 017 reads rows (``fetchall``), which offline mode
  cannot do: ``alembic upgrade 035 --sql`` from an empty history fails there.

The image metadata must say linux/amd64, carry the revision label and
``SPA_DIST_DIR=/app/spa``, and run as ``neurostack``. A JSON summary (timings,
image size, build warnings, per-check status) goes to stdout. Exit status: 0
when the build and every check pass, 1 otherwise, 2 when Docker is unusable.
"""

from __future__ import annotations

import argparse
import json
import re
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
# Dockerfile's base stage moves pip and setuptools to their locked versions).
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
    problems += [
        f"{name} {found[name]} is not in the lock's main group"
        for name in sorted(set(found) - set(expected) - BASE_DISTRIBUTIONS)
    ]
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

    return [
        Check("imports", ("python", "-c", IMPORTS), _exit_zero),
        Check("site-packages", ("python", "-c", SITE_PACKAGES), site_packages),
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
        help="the single Alembic head the image must carry, e.g. 035",
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
