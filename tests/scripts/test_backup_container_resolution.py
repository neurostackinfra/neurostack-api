"""scripts/backup.sh finds the PostgreSQL container without loading any compose file.

On the production (VPS) checkout the development docker-compose.yml refuses to
load unless NEUROSTACK_DEV_STACK is set, and docker-compose.production.yml needs
variables the production .env cannot carry. So backup.sh resolves the running
container of the checkout's Compose project by its labels with plain
``docker ps`` and dumps through ``docker exec``. These tests run on a VPS-shaped
checkout with a fake ``docker`` that records every call and fails any
``docker compose`` call; no PostgreSQL and no Docker daemon are needed.
"""

from __future__ import annotations

import gzip
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = ROOT / "scripts" / "backup.sh"
DEV_COMPOSE = ROOT / "docker-compose.yml"
CONTAINER = "4f" * 32
DUMP = "-- PostgreSQL database dump\nSELECT 'backup-sentinel';\n"
VPS_ENV = (
    "ENVIRONMENT=production\n"
    "POSTGRES_USER=clinicas_owner\n"
    "POSTGRES_PASSWORD=Pg9!Unique-Database-Secret-2026\n"
    "POSTGRES_DB=clinicas_core\n"
)
COMMANDS = (
    "age",
    "age-keygen",
    "bash",
    "chmod",
    "date",
    "find",
    "gzip",
    "ln",
    "mkdir",
    "mktemp",
    "rm",
    "sync",
)

pytestmark = pytest.mark.skipif(
    any(shutil.which(command) is None for command in COMMANDS),
    reason="backup.sh prerequisites (age, gzip, coreutils) are unavailable",
)


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for command in COMMANDS:
        target = shutil.which(command)
        assert target is not None, command
        (bin_dir / command).symlink_to(target)
    _executable(
        bin_dir / "docker",
        "#!/usr/bin/env bash\n"
        'printf "%q " "$@" >> "$DOCKER_LOG"\n'
        'printf "\\n" >> "$DOCKER_LOG"\n'
        'case "$1" in\n'
        "  compose)\n"
        # What the real CLI does with the dev file on the VPS checkout.
        '    echo "error while interpolating x-neurostack-dev-stack: required'
        ' variable NEUROSTACK_DEV_STACK is missing a value" >&2\n'
        "    exit 1 ;;\n"
        "  ps)\n"
        '    if [[ -n "${FAKE_PS_STDERR:-}" ]]; then echo "$FAKE_PS_STDERR" >&2; fi\n'
        '    if [[ -n "${FAKE_PS_IDS:-}" ]]; then printf "%s\\n" $FAKE_PS_IDS; fi\n'
        '    exit "${FAKE_PS_STATUS:-0}" ;;\n'
        "  exec)\n"
        '    printf "%s" "$FAKE_DUMP"\n'
        "    exit 0 ;;\n"
        "esac\n"
        "exit 64\n",
    )
    return bin_dir


def _checkout(tmp_path: Path, name: str) -> Path:
    """A production-shaped checkout: the dev compose file and a VPS .env."""
    checkout = tmp_path / "srv" / name
    checkout.mkdir(parents=True)
    shutil.copy2(DEV_COMPOSE, checkout / "docker-compose.yml")
    (checkout / ".env").write_text(VPS_ENV, encoding="utf-8")
    return checkout


def _age_key(tmp_path: Path) -> tuple[Path, str]:
    identity = tmp_path / "identity.txt"
    subprocess.run(["age-keygen", "-o", str(identity)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return identity, recipient


def _backup(
    tmp_path: Path,
    checkout: Path,
    *,
    ps_ids: str = CONTAINER,
    extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], Path, Path]:
    bin_dir = _fake_bin(tmp_path)
    identity, recipient = _age_key(tmp_path)
    log = tmp_path / "docker.log"
    backup_dir = tmp_path / "backups"
    env = {
        "PATH": str(bin_dir),
        "HOME": str(tmp_path),
        "BACKUP_DIR": str(backup_dir),
        "BACKUP_AGE_RECIPIENT": recipient,
        "POSTGRES_USER": "clinicas_owner",
        "POSTGRES_DB": "clinicas_core",
        "DOCKER_LOG": str(log),
        "FAKE_PS_IDS": ps_ids,
        "FAKE_DUMP": DUMP,
        **(extra or {}),
    }
    result = subprocess.run(
        [str(bin_dir / "bash"), str(BACKUP_SCRIPT)],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    calls = (
        [shlex.split(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, calls, backup_dir, identity


def _ps_call(project: str, service: str = "postgres") -> list[str]:
    return [
        "ps",
        "--quiet",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={service}",
        "--filter",
        "label=com.docker.compose.oneoff=False",
    ]


def test_backup_on_a_vps_checkout_never_loads_a_compose_file(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    result, calls, backup_dir, identity = _backup(tmp_path, checkout)

    assert result.returncode == 0, result.stdout + result.stderr
    assert [call for call in calls if call[0] == "compose"] == []
    assert calls == [
        _ps_call("neurostack-api"),
        ["exec", "-i", CONTAINER, "pg_dump", "-U", "clinicas_owner", "clinicas_core"],
    ]
    published = list(backup_dir.glob("postgres_*.sql.gz.age"))
    assert len(published) == 1
    assert re.fullmatch(r"postgres_\d{8}_\d{6}\.sql\.gz\.age", published[0].name)
    assert [item for item in backup_dir.iterdir() if item != published[0]] == []
    plaintext = subprocess.run(
        ["age", "--decrypt", "--identity", str(identity), str(published[0])],
        check=True,
        capture_output=True,
    ).stdout
    assert gzip.decompress(plaintext).decode("utf-8") == DUMP


@pytest.mark.parametrize(
    ("directory", "project"),
    [
        # Measured with `docker compose config --format json` (Compose v2).
        ("neurostack-api", "neurostack-api"),
        ("NeuroStack.API", "neurostackapi"),
        ("my api dir", "myapidir"),
        ("_lead_under", "lead_under"),
        ("-lead-dash", "lead-dash"),
        ("a.b_c-D", "ab_c-d"),
        ("...dots", "dots"),
        ("123abc", "123abc"),
    ],
)
def test_project_name_follows_compose_default_derivation(
    tmp_path: Path, directory: str, project: str
) -> None:
    checkout = _checkout(tmp_path, directory)
    result, calls, _, _ = _backup(tmp_path, checkout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0] == _ps_call(project)


def test_project_name_uses_the_physical_checkout_directory(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    link = tmp_path / "Linked.Checkout"
    link.symlink_to(checkout)
    # A shell that cd'd through the symlink exports the logical PWD;
    # deploy-vps.sh resolves the checkout with readlink -f, so Compose named
    # the production project after the physical directory.
    result, calls, _, _ = _backup(tmp_path, link, extra={"PWD": str(link)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0] == _ps_call("neurostack-api")


@pytest.mark.skipif(
    not Path("/usr/bin/docker").is_file(),
    reason="authoritative Compose renderer is unavailable",
)
@pytest.mark.parametrize("directory", ["NeuroStack.API", "my api dir", "-lead-dash"])
def test_project_name_matches_the_real_compose_cli(
    tmp_path: Path, directory: str
) -> None:
    """``docker compose config`` renders client-side; no daemon is contacted."""
    work = tmp_path / directory
    work.mkdir()
    (work / "docker-compose.yml").write_text(
        "services:\n  x:\n    image: busybox\n", encoding="utf-8"
    )
    rendered = subprocess.run(
        ["/usr/bin/docker", "compose", "config", "--format", "json"],
        cwd=work,
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    expected = re.search(r'"name":\s*"([^"]+)"', rendered)
    assert expected
    checkout = _checkout(tmp_path / "backup", directory)
    result, calls, _, _ = _backup(tmp_path / "backup", checkout)
    assert result.returncode == 0, result.stderr
    assert calls[0] == _ps_call(expected.group(1))


def test_compose_project_name_override_is_used(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    result, calls, _, _ = _backup(
        tmp_path, checkout, extra={"COMPOSE_PROJECT_NAME": "prod_stack-2"}
    )
    assert result.returncode == 0, result.stderr
    assert calls[0] == _ps_call("prod_stack-2")


def test_postgres_service_override_is_used(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    result, calls, _, _ = _backup(
        tmp_path, checkout, extra={"POSTGRES_SERVICE": "database"}
    )
    assert result.returncode == 0, result.stderr
    assert calls[0] == _ps_call("neurostack-api", service="database")


@pytest.mark.parametrize("name", ["Prod", "prod.x", "_x", "-x", "a b"])
def test_invalid_compose_project_name_fails_before_docker(
    tmp_path: Path, name: str
) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    result, calls, backup_dir, _ = _backup(
        tmp_path, checkout, extra={"COMPOSE_PROJECT_NAME": name}
    )
    assert result.returncode == 1
    assert "COMPOSE_PROJECT_NAME is not a valid Compose project name" in result.stderr
    assert calls == []
    assert not backup_dir.exists()


def test_non_ascii_checkout_requires_an_explicit_project_name(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "Ünïcode.Ñame")
    result, calls, _, _ = _backup(tmp_path, checkout)
    assert result.returncode == 1
    assert "set COMPOSE_PROJECT_NAME" in result.stderr
    assert calls == []
    explicit, calls, _, _ = _backup(
        tmp_path / "again", checkout, extra={"COMPOSE_PROJECT_NAME": "unicode"}
    )
    assert explicit.returncode == 0, explicit.stderr
    assert calls[0] == _ps_call("unicode")


def test_underivable_project_name_fails(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "...")
    result, calls, _, _ = _backup(tmp_path, checkout)
    assert result.returncode == 1
    assert "set COMPOSE_PROJECT_NAME" in result.stderr
    assert calls == []


@pytest.mark.parametrize(
    ("ids", "count"),
    [
        ("", 0),
        (f"{CONTAINER} {'5e' * 32}", 2),
        (f"{CONTAINER} {'5e' * 32} {'6d' * 32}", 3),
    ],
)
def test_zero_or_several_containers_fail_visibly(
    tmp_path: Path, ids: str, count: int
) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    result, calls, backup_dir, _ = _backup(tmp_path, checkout, ps_ids=ids)
    assert result.returncode == 1
    assert (
        "Expected exactly one running 'postgres' container in Compose project "
        f"'neurostack-api', found {count}"
    ) in result.stderr
    assert calls == [_ps_call("neurostack-api")]
    assert not backup_dir.exists()


def test_docker_ps_failure_is_reported_not_hidden(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    daemon_error = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    result, calls, backup_dir, _ = _backup(
        tmp_path,
        checkout,
        extra={"FAKE_PS_STATUS": "1", "FAKE_PS_STDERR": daemon_error},
    )
    assert result.returncode == 1
    assert daemon_error in result.stderr
    assert "docker ps failed; cannot locate the PostgreSQL container" in result.stderr
    assert [call[0] for call in calls] == ["ps"]
    assert not backup_dir.exists()


@pytest.mark.parametrize("ids", ["not-an-id", "4F" * 32, "abc", "--all"])
def test_unexpected_container_id_fails(tmp_path: Path, ids: str) -> None:
    checkout = _checkout(tmp_path, "neurostack-api")
    result, calls, _, _ = _backup(tmp_path, checkout, ps_ids=ids)
    assert result.returncode == 1
    assert "unexpected container id" in result.stderr
    assert [call[0] for call in calls] == ["ps"]


def _code_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_backup_script_never_invokes_compose() -> None:
    for line in _code_lines(BACKUP_SCRIPT):
        assert re.search(r"\bdocker\s+compose\b|\bdocker-compose\s", line) is None, line


def test_pg_dump_pipeline_is_unchanged_apart_from_the_exec_form() -> None:
    text = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert (
        'if ! docker exec -i "$postgres_container" \\\n'
        '    pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" 2>/dev/null \\\n'
        "    | gzip -c \\\n"
        '    | age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" \\\n'
        '        >"$temp_file" 2>/dev/null; then\n'
        '    error "Encrypted PostgreSQL backup pipeline failed"\n'
        "    exit 1\n"
        "fi\n"
    ) in text


@pytest.mark.parametrize(
    "path",
    ["scripts/backup.sh", "scripts/restore_backup.sh", "scripts/deploy-vps.sh"],
)
def test_production_scripts_never_enable_the_dev_stack(path: str) -> None:
    for line in _code_lines(ROOT / path):
        assert "NEUROSTACK_DEV_STACK" not in line, line
