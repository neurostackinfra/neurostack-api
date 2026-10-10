"""The authoritative VPS entrypoint must have no pre-gate effects."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/deploy-vps.sh"
DATABASE_BUDGET_CHECKER = ROOT / "scripts/check_database_connection_budget.py"


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
    assert any("alembic upgrade head" in line for line in compose_calls)


def test_database_budget_gate_runs_before_any_docker_effect() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    budget = text.index('/usr/bin/python3 "$DATABASE_BUDGET_CHECKER"')
    pull = text.index(" pull api")
    up = text.index(" up -d")
    migration = text.index("alembic upgrade head")
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
