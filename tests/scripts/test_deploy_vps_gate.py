"""The authoritative VPS entrypoint must have no pre-gate effects."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy-vps.sh"
DATABASE_BUDGET_CHECKER = ROOT / "scripts/check_database_connection_budget.py"
PIN_LINE = re.compile(r'^readonly APPROVED_ALEMBIC_REVISION="[^"\n]*"$', re.M)
VALID_ENV = (
    "SECRET_KEY=Sk9!Unique-Application-Secret-2026xx\n"
    "POSTGRES_USER=prod_user\nPOSTGRES_PASSWORD=Pg9!Unique-Database-Secret-2026\n"
    "POSTGRES_DB=prod_data\nREDIS_PASSWORD=Rd8!Unique-Cache-Secret-2026xx\n"
    "QDRANT_API_KEY=Qd7!Unique-Vector-Secret-2026xxx\n"
    'ALLOWED_ORIGINS=["https://app.production.invalid"]\n'
    'ALLOWED_HOSTS=["api.production.invalid"]\n'
)


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _deploy_fixture(tmp_path: Path, env_text: str) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    copied = scripts / "deploy-vps.sh"
    copied.write_bytes(SCRIPT.read_bytes())
    copied.chmod(copied.stat().st_mode | stat.S_IXUSR)
    digest = "registry.example/neurostack/api@sha256:" + "a" * 64
    (scripts / "validate_provider_availability_evidence.py").write_text(
        f"#!/usr/bin/env python3\nprint({digest!r})\n", encoding="utf-8"
    )
    (root / "docker-compose.production.yml").write_text("services: {}\n")
    (root / ".env").write_text(env_text, encoding="utf-8")
    return copied, root / ".env"


def _recording_deploy_fixture(
    tmp_path: Path,
    pinned_revision: str | None = None,
    image_heads: str = "035 (head)\n",
    image_heads_exit: int = 0,
) -> tuple[Path, Path, Path]:
    """A full fixture whose copied script runs a recording fake docker.

    The Docker test seam rewrites the copied script's literal ``/usr/bin/docker``
    to a fake in the fixture; the production script has no override for it.
    ``pinned_revision`` replaces the pinned constant to simulate tampering, and
    the fake answers ``alembic heads`` with ``image_heads`` and
    ``image_heads_exit`` to stand in for the pulled image.
    """
    copied, _env_file = _deploy_fixture(tmp_path, VALID_ENV)
    scripts = copied.parent
    docker_log = tmp_path / "docker-argv.log"
    checker_marker = tmp_path / "checker-called"
    heads_answer = tmp_path / "image-heads.txt"
    heads_answer.write_text(image_heads, encoding="utf-8")
    fake_docker = tmp_path / "fake-docker"
    _executable(
        fake_docker,
        "#!/bin/sh\n"
        f'for arg in "$@"; do printf \'%s\\037\' "$arg" >> {docker_log}; done\n'
        f"printf '\\n' >> {docker_log}\n"
        'case " $* " in *" alembic heads "*)\n'
        f"    cat {heads_answer}\n"
        f"    exit {image_heads_exit}\n"
        "    ;;\n"
        "esac\n",
    )
    digest = "registry.example/neurostack/api@sha256:" + "a" * 64
    (scripts / "validate_provider_availability_evidence.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        f"pathlib.Path({str(checker_marker)!r}).touch()\n"
        f"print({digest!r})\n",
        encoding="utf-8",
    )
    (scripts / "check_database_connection_budget.py").write_text(
        "#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8"
    )
    text = copied.read_text(encoding="utf-8")
    assert "/usr/bin/docker" in text
    text = text.replace("/usr/bin/docker", str(fake_docker))
    if pinned_revision is not None:
        text, replaced = PIN_LINE.subn(
            lambda _match: f'readonly APPROVED_ALEMBIC_REVISION="{pinned_revision}"',
            text,
        )
        assert replaced == 1
    copied.write_text(text, encoding="utf-8")
    return copied, docker_log, checker_marker


def _recorded_docker_calls(docker_log: Path) -> list[list[str]]:
    if not docker_log.exists():
        return []
    return [
        line.split("\x1f")[:-1]
        for line in docker_log.read_text(encoding="utf-8").splitlines()
    ]


def _compose_prefix(copied: Path) -> list[str]:
    return [
        "compose",
        "-f",
        str(copied.parents[1] / "docker-compose.production.yml"),
        "--env-file",
        str(copied.parents[1] / ".env"),
    ]


def _run(
    copied: Path, cwd: Path, locale: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"]}
    if locale is not None:
        env["LC_ALL"] = locale
    return subprocess.run(
        ["/bin/bash", str(copied)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


IMAGE_HEADS_CALL = ["run", "--rm", "--no-deps", "-T", "api", "alembic", "heads"]


@pytest.mark.parametrize("revision", ("035", "036"))
def test_deploy_migrates_exactly_the_pinned_revision(
    tmp_path: Path, revision: str
) -> None:
    # 036 proves the pin is interpolated into both the head check and the
    # migration rather than hard-coded; production pins 035.
    copied, docker_log, checker_marker = _recording_deploy_fixture(
        tmp_path,
        pinned_revision=None if revision == "035" else revision,
        image_heads=f"{revision} (head)\n",
    )

    completed = _run(copied, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert checker_marker.exists()
    calls = _recorded_docker_calls(docker_log)
    compose_prefix = _compose_prefix(copied)
    assert all(call[:5] == compose_prefix for call in calls)
    migration = ["exec", "-T", "api", "alembic", "upgrade", revision]
    assert [call[5:] for call in calls] == [
        ["version"],
        ["pull", "api"],
        IMAGE_HEADS_CALL,
        ["up", "-d"],
        migration,
        ["ps"],
    ]
    assert [call[5:] for call in calls if "upgrade" in call] == [migration]
    # The only head-like argv token is the read-only ``alembic heads`` check.
    head_tokens = [
        token for call in calls for token in call[5:] if "head" in token.lower()
    ]
    assert head_tokens == ["heads"]


@pytest.mark.parametrize(
    ("image_heads", "image_heads_exit", "shown"),
    (
        ("036 (head)\n", 0, "'036 (head)'"),
        ("037 (head)\n", 0, "'037 (head)'"),
        ("035 (head)\n036 (head)\n", 0, "'035 (head) 036 (head)'"),
        ("036 (head)\n035 (head)\n", 0, "'036 (head) 035 (head)'"),
        ("035 (head)\n", 1, "'unreadable (alembic heads failed)'"),
        ("", 1, "'unreadable (alembic heads failed)'"),
        ("", 0, "'<none>'"),
        ("035\n", 0, "'035'"),
        ("035 (effective head)\n", 0, "'035 (effective head)'"),
        ("0350 (head)\n", 0, "'0350 (head)'"),
        (" 035 (head)\n", 0, "' 035 (head)'"),
    ),
)
def test_deploy_refuses_an_image_whose_alembic_head_is_not_the_pin(
    tmp_path: Path, image_heads: str, image_heads_exit: int, shown: str
) -> None:
    copied, docker_log, _checker_marker = _recording_deploy_fixture(
        tmp_path, image_heads=image_heads, image_heads_exit=image_heads_exit
    )

    completed = _run(copied, tmp_path)

    assert completed.returncode != 0
    assert completed.stderr.endswith(
        f"deploy blocked: image alembic head {shown} is not the approved "
        "revision 035; bump APPROVED_ALEMBIC_REVISION only with the "
        "owner-approved migration window\n"
    ), completed.stderr
    calls = _recorded_docker_calls(docker_log)
    # Nothing started: the head check is the last Docker command.
    assert [call[5:] for call in calls] == [
        ["version"],
        ["pull", "api"],
        IMAGE_HEADS_CALL,
    ]


def test_deploy_refuses_an_image_head_that_differs_from_a_bumped_pin(
    tmp_path: Path,
) -> None:
    copied, docker_log, _checker_marker = _recording_deploy_fixture(
        tmp_path, pinned_revision="036", image_heads="035 (head)\n"
    )

    completed = _run(copied, tmp_path)

    assert completed.returncode != 0
    assert "is not the approved revision 036;" in completed.stderr
    assert not any("up" in call for call in _recorded_docker_calls(docker_log))


@pytest.mark.parametrize("locale", ("C", "C.UTF-8"))
@pytest.mark.parametrize(
    "tampered",
    (
        "",
        "head",
        "heads",
        "HEAD",
        "base",
        "+1",
        "-1",
        "035+1",
        "035; rm",
        "035 ",
        " 035",
        "03 5",
        "035\\n",
        "035@head",
        "035:head",
        "--sql",
        "-x",
        "head-1",
        "035\n",
        "035\nhead",
        "035\r",
        "035\t",
        "০৩৫",  # Bengali digits 035
        "０３５",  # fullwidth digits 035
    ),
)
def test_deploy_refuses_a_tampered_pinned_revision_before_any_effect(
    tmp_path: Path, tampered: str, locale: str
) -> None:
    copied, docker_log, checker_marker = _recording_deploy_fixture(
        tmp_path, pinned_revision=tampered
    )

    completed = _run(copied, tmp_path, locale=locale)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "deploy blocked: APPROVED_ALEMBIC_REVISION" in completed.stderr
    assert not checker_marker.exists()
    assert not docker_log.exists()


def test_checker_invocation_is_literal_first_command_in_main() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    body = text.split("main() {", 1)[1].split("\n}", 1)[0]
    commands = [line.strip() for line in body.splitlines() if line.strip()]
    assert commands[0] == (
        'GEMINI_IMAGE_DIGEST="$(/usr/bin/python3 "$CHECKER" --phase release)" || return 1'
    )
    checker = (ROOT / "scripts/validate_provider_availability_evidence.py").read_text()
    assert "from yaml" not in checker
    assert "import yaml" not in checker


def test_database_budget_checker_runs_with_stdlib_only_import_isolation() -> None:
    checker = DATABASE_BUDGET_CHECKER.read_text(encoding="utf-8")
    assert "import yaml" not in checker
    completed = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            str(DATABASE_BUDGET_CHECKER),
            "--manifest",
            str(ROOT / "config/database-connection-budgets.json"),
            "--profile",
            "vps-compose",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"total": 20' in completed.stdout


def test_foreign_cwd_cannot_substitute_checker_python_env_or_docker(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    foreign = tmp_path / "foreign"
    (foreign / "scripts").mkdir(parents=True)
    env_file = foreign / ".env"
    env_file.write_text("PATH=malicious\n", encoding="utf-8")
    checker_marker = tmp_path / "foreign-checker-called"
    python_marker = tmp_path / "foreign-python-called"
    marker = tmp_path / "docker-called"
    _executable(
        foreign / "scripts/validate_provider_availability_evidence.py",
        f"#!/bin/sh\ntouch {checker_marker}\nexit 0\n",
    )
    _executable(bin_dir / "python3", f"#!/bin/sh\ntouch {python_marker}\nexit 0\n")
    _executable(bin_dir / "docker", f"#!/bin/sh\ntouch {marker}\nexit 0\n")

    completed = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=foreign,
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "Gemini receipt is absent" in completed.stderr
    assert not checker_marker.exists()
    assert not python_marker.exists()
    assert not marker.exists()
    assert env_file.read_text() == "PATH=malicious\n"


def test_missing_python_fails_before_docker_or_env(tmp_path: Path) -> None:
    if Path("/usr/bin/python3").exists():
        return
    completed = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=ROOT,
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "python3" in completed.stderr
    assert completed.stdout == ""


def test_weak_runtime_environment_fails_before_docker_without_mutation(
    tmp_path: Path,
) -> None:
    weak = (
        "SECRET_KEY=dev-secret-key-change-in-production\n"
        "POSTGRES_USER=prod_user\nPOSTGRES_PASSWORD=Pg9!Unique-Database-Secret-2026\n"
        "POSTGRES_DB=prod_data\nREDIS_PASSWORD=Rd8!Unique-Cache-Secret-2026xx\n"
        "QDRANT_API_KEY=Qd7!Unique-Vector-Secret-2026xxx\n"
        'ALLOWED_ORIGINS=["https://app.production.invalid"]\n'
        'ALLOWED_HOSTS=["api.production.invalid"]\n'
    )
    copied, env_file = _deploy_fixture(tmp_path, weak)
    before = env_file.read_bytes()

    completed = subprocess.run(
        ["/bin/bash", str(copied)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "SECRET_KEY" in completed.stderr
    assert env_file.read_bytes() == before


def test_env_is_data_only_and_cannot_execute_or_steer_runtime(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    valid_prefix = (
        "SECRET_KEY=Sk9!Unique-Application-Secret-2026xx\n"
        "POSTGRES_USER=prod_user\nPOSTGRES_PASSWORD=Pg9!Unique-Database-Secret-2026\n"
        "POSTGRES_DB=prod_data\nREDIS_PASSWORD=Rd8!Unique-Cache-Secret-2026xx\n"
        "QDRANT_API_KEY=Qd7!Unique-Vector-Secret-2026xxx\n"
        'ALLOWED_ORIGINS=["https://app.production.invalid"]\n'
        'ALLOWED_HOSTS=["api.production.invalid"]\n'
    )
    for malicious in (
        f"SECRET_KEY=$(touch${{IFS}}{marker})\n",
        f"SECRET_KEY=`touch${{IFS}}{marker}`\n",
        "PATH=/tmp/hostile\n",
        "DOCKER_HOST=tcp://attacker.invalid:2375\n",
        "UNKNOWN=value\n",
        "evil()=value\n",
        "DEBUG=false;true\n",
    ):
        case_dir = tmp_path / str(abs(hash(malicious)))
        copied, env_file = _deploy_fixture(case_dir, valid_prefix + malicious)
        before = env_file.read_bytes()
        completed = subprocess.run(
            ["/bin/bash", str(copied)],
            cwd=tmp_path,
            env={"PATH": "/tmp/hostile", "DOCKER_HOST": "tcp://caller.invalid:2375"},
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode != 0
        assert "line" in completed.stderr or "unknown key" in completed.stderr
        assert env_file.read_bytes() == before
        assert not marker.exists()


@pytest.mark.parametrize(
    ("replacement", "message"),
    (
        ("POSTGRES_USER=postgres", "repository default"),
        ("POSTGRES_DB=neurostack", "repository default"),
        ("SECRET_KEY=A1!A1!A1!A1!A1!A1!A1!A1!A1!A1!A1!", "distinct characters"),
        ("REDIS_PASSWORD=short-A1!", "24 characters"),
        ("QDRANT_API_KEY=changeme-Strong-A1!234567890123456", "repository default"),
    ),
)
def test_runtime_rejects_default_short_and_low_diversity_values(
    tmp_path: Path, replacement: str, message: str
) -> None:
    values = {
        "SECRET_KEY": "Sk9!Unique-Application-Secret-2026xx",
        "POSTGRES_USER": "prod_user",
        "POSTGRES_PASSWORD": "Pg9!Unique-Database-Secret-2026",
        "POSTGRES_DB": "prod_data",
        "REDIS_PASSWORD": "Rd8!Unique-Cache-Secret-2026xx",
        "QDRANT_API_KEY": "Qd7!Unique-Vector-Secret-2026xxx",
        "ALLOWED_ORIGINS": '["https://app.production.invalid"]',
        "ALLOWED_HOSTS": '["api.production.invalid"]',
    }
    key, value = replacement.split("=", 1)
    values[key] = value
    copied, _env_file = _deploy_fixture(
        tmp_path, "".join(f"{name}={item}\n" for name, item in values.items())
    )
    completed = subprocess.run(
        ["/bin/bash", str(copied)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert message in completed.stderr


def test_runtime_rejects_reused_secret_values(tmp_path: Path) -> None:
    repeated = "Sk9!Unique-Application-Secret-2026xx"
    env_text = (
        f"SECRET_KEY={repeated}\n"
        "POSTGRES_USER=prod_user\n"
        f"POSTGRES_PASSWORD={repeated}\n"
        "POSTGRES_DB=prod_data\n"
        "REDIS_PASSWORD=Rd8!Unique-Cache-Secret-2026xx\n"
        "QDRANT_API_KEY=Qd7!Unique-Vector-Secret-2026xxx\n"
        'ALLOWED_ORIGINS=["https://app.production.invalid"]\n'
        'ALLOWED_HOSTS=["api.production.invalid"]\n'
    )
    copied, _env_file = _deploy_fixture(tmp_path, env_text)
    completed = subprocess.run(
        ["/bin/bash", str(copied)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "must be distinct" in completed.stderr


def test_deploy_never_builds_mutates_secrets_or_uses_default_compose() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "docker compose build" not in text
    assert "docker compose down" not in text
    assert "cp env.example" not in text
    assert "sed -i" not in text
    compose_calls = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("/usr/bin/docker compose")
    ]
    assert compose_calls
    assert all(
        '-f "$COMPOSE_FILE" --env-file "$ENV_FILE"' in line for line in compose_calls
    )
    assert any(" pull api" in line for line in compose_calls)
    assert any(" up -d" in line for line in compose_calls)
    assert any(
        line.endswith('exec -T api alembic upgrade "$APPROVED_ALEMBIC_REVISION"')
        for line in compose_calls
    )
    assert not re.search(r"upgrade\s+heads?\b", text, re.IGNORECASE)


def test_database_budget_gate_runs_before_any_docker_effect() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    budget = text.index('/usr/bin/python3 "$DATABASE_BUDGET_CHECKER"')
    pull = text.index(" pull api")
    up = text.index(" up -d")
    migration = text.index('alembic upgrade "$APPROVED_ALEMBIC_REVISION"')
    assert budget < pull < up < migration
    for argument in (
        '--manifest "$DATABASE_BUDGET_MANIFEST"',
        "--profile vps-compose",
        "--verify-vps-compose",
        '--compose "$COMPOSE_FILE"',
        '--dockerfile "$DOCKERFILE"',
        '--env-file "$ENV_FILE"',
    ):
        assert argument in text
