"""Contract of the production image smoke (scripts/production_image_smoke.py).

The unit tests pin what the smoke builds and checks without Docker and always
run. The end-to-end test builds the image from the working tree and runs every
check; it is OPT-IN: it runs only with ``RUN_DOCKER_IMAGE_SMOKE=1`` (and then
fails, never skips, when Docker is unusable), because it takes minutes and
needs the build network (Docker Hub, Debian mirrors, PyPI, the npm registry)
unless the build cache already holds every layer.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
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

EXPECTED_CHECKS = {
    "imports": ("python", "-c", "import src.api.main, boto3, botocore"),
    "alembic-heads": ("alembic", "heads"),
    "spa-index": ("test", "-f", "/app/spa/index.html"),
    "alembic-offline-sql": ("alembic", "upgrade", "032:035", "--sql"),
}


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
    rows += [["pip", "26.2.1", SITE], ["setuptools", "84.0.0", SITE]]
    rows += [["wheel", "0.46.3", SITE], *(extra or [])]
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


# --- Image metadata ----------------------------------------------------------


def _image(**changes: Any) -> dict[str, Any]:
    image: dict[str, Any] = {
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "User": "neurostack",
            "Env": ["PATH=/usr/local/bin", "SPA_DIST_DIR=/app/spa"],
            "Labels": {"org.opencontainers.image.revision": "abc123"},
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
    ],
    ids=["ok", "arch", "no-label", "default-label", "null-labels", "spa-env", "root"],
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


IMAGE_ID = "sha256:" + "ab" * 32


class FakeDocker:
    """Answers the build, the inspect and every check of ``smoke.smoke``."""

    def __init__(self, image: dict[str, Any], build_code: int = 0) -> None:
        self.image = image
        self.build_code = build_code
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "buildx", "build"]:
            return _completed("#1 DONE 0.1s\n", returncode=self.build_code)
        if argv[:3] == ["docker", "image", "inspect"]:
            return _completed(json.dumps([self.image]))
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
    assert [check["status"] for check in summary.checks] == ["pass"]
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
    assert [check["name"] for check in summary["checks"]] == list(_checks())
    assert summary["image_problems"] == []
