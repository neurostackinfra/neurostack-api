"""Destructive-safety tests for encrypted PostgreSQL backup and restore."""

from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = ROOT / "scripts" / "backup.sh"
RESTORE_SCRIPT = ROOT / "scripts" / "restore_backup.sh"
CHILD_NAME = re.compile(r"^neurostack_backup_smoke_[0-9a-f]{12}_(source|target)$")


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def _sync_postgres_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail("POSTGRES_SMOKE_DATABASE_URL must use PostgreSQL", pytrace=False)
    if (url.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(
            "POSTGRES_SMOKE_DATABASE_URL must use a loopback host", pytrace=False
        )
    if url.database != "postgres":
        pytest.fail(
            "POSTGRES_SMOKE_DATABASE_URL must target the postgres admin database",
            pytrace=False,
        )
    return url.set(drivername="postgresql")


def _render_url(url: URL) -> str:
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="module", autouse=True)
def postgres_admin_url() -> URL:
    raw_url = os.environ.get("POSTGRES_SMOKE_DATABASE_URL")
    if not raw_url:
        pytest.fail(
            "POSTGRES_SMOKE_DATABASE_URL is required; backup tests never skip",
            pytrace=False,
        )
    return _sync_postgres_url(raw_url)


@dataclass(frozen=True)
class DisposableDatabases:
    source_name: str
    source_url: str
    target_url: str
    sentinel: str


def _psql(url: str, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", url, "--set=ON_ERROR_STOP=1", "--tuples-only", "--no-align"],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )


def _drop_disposable_databases(admin_url: str, names: tuple[str, ...]) -> None:
    failures: list[str] = []
    for name in names:
        if not CHILD_NAME.fullmatch(name):
            failures.append(f"refused unsafe database name: {name}")
            continue
        result = _psql(admin_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE);')
        if result.returncode != 0:
            failures.append(f"{name}: {result.stderr.strip() or 'drop failed'}")
    if failures:
        pytest.fail(
            "disposable database cleanup failed after all attempts: "
            + "; ".join(failures),
            pytrace=False,
        )


def test_database_cleanup_attempts_every_drop_before_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = (
        "neurostack_backup_smoke_aaaaaaaaaaaa_source",
        "neurostack_backup_smoke_aaaaaaaaaaaa_target",
    )
    attempted: list[str] = []

    def fake_psql(url: str, sql: str) -> subprocess.CompletedProcess[str]:
        attempted.append(sql)
        return subprocess.CompletedProcess(
            ["psql", url],
            1 if len(attempted) == 1 else 0,
            "",
            "simulated first drop failure" if len(attempted) == 1 else "",
        )

    monkeypatch.setattr(sys.modules[__name__], "_psql", fake_psql)

    with pytest.raises(
        pytest.fail.Exception, match="cleanup failed after all attempts"
    ):
        _drop_disposable_databases("postgresql://admin", names)

    assert len(attempted) == 2
    assert names[0] in attempted[0]
    assert names[1] in attempted[1]


@pytest.fixture()
def disposable_databases(postgres_admin_url: URL) -> DisposableDatabases:
    token = uuid4().hex[:12]
    source_name = f"neurostack_backup_smoke_{token}_source"
    target_name = f"neurostack_backup_smoke_{token}_target"
    assert CHILD_NAME.fullmatch(source_name)
    assert CHILD_NAME.fullmatch(target_name)
    admin_url = _render_url(postgres_admin_url)
    source_url = _render_url(postgres_admin_url.set(database=source_name))
    target_url = _render_url(postgres_admin_url.set(database=target_name))
    sentinel = f"media-backup-sentinel-{uuid4().hex}"

    try:
        for name in (source_name, target_name):
            result = _psql(admin_url, f'CREATE DATABASE "{name}";')
            assert result.returncode == 0, result.stderr
        result = _psql(
            source_url,
            "CREATE TABLE backup_sentinel (value text PRIMARY KEY);\n"
            f"INSERT INTO backup_sentinel VALUES ('{sentinel}');\n",
        )
        assert result.returncode == 0, result.stderr
        yield DisposableDatabases(source_name, source_url, target_url, sentinel)
    finally:
        _drop_disposable_databases(admin_url, (source_name, target_name))


@dataclass(frozen=True)
class AgeMaterial:
    identity: Path
    recipient: str


@pytest.fixture()
def age_material(tmp_path: Path) -> AgeMaterial:
    identity = tmp_path / "test-only-age-identity.txt"
    generated = subprocess.run(
        ["age-keygen", "-o", str(identity)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    recipient = subprocess.run(
        ["age-keygen", "-y", str(identity)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert recipient.returncode == 0, recipient.stderr
    assert stat.S_IMODE(identity.stat().st_mode) == 0o600
    return AgeMaterial(identity, recipient.stdout.strip())


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _install_fake_docker(fake_bin: Path) -> Path:
    dump_marker = fake_bin / "pg_dump.called"
    real_pg_dump = shutil.which("pg_dump")
    assert real_pg_dump is not None
    # backup.sh must never load a compose file (on a production checkout the
    # development docker-compose.yml refuses to load): `compose` fails, `ps`
    # lists one container and `exec -i <that id>` runs the dump.
    _write_executable(
        fake_bin / "docker",
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'if [[ "$1" == "compose" ]]; then\n'
        '  echo "compose must not be called" >&2\n'
        "  exit 97\n"
        "fi\n"
        'if [[ "$1" == "ps" ]]; then printf \'%064d\\n\' 7; exit 0; fi\n'
        'if [[ "$1" == "exec" && "$2" == "-i" && "$3" == "$(printf \'%064d\' 7)" ]]; then\n'
        '  : > "$PG_DUMP_MARKER"\n'
        '  if [[ -n "${BACKUP_TEST_STALL_MARKER:-}" ]]; then\n'
        '    : > "$BACKUP_TEST_STALL_MARKER"\n'
        "    while true; do sleep 1; done\n"
        "  fi\n"
        '  if [[ -n "${BACKUP_TEST_BARRIER_DIR:-}" ]]; then\n'
        '    : > "$BACKUP_TEST_BARRIER_DIR/$$"\n'
        '    while [[ "$(find "$BACKUP_TEST_BARRIER_DIR" -type f | wc -l)" -lt 2 ]]; do sleep 0.01; done\n'
        "  fi\n"
        '  if [[ "${FAIL_PG_DUMP:-0}" == "1" ]]; then\n'
        "    printf 'partial dump'\n"
        "    exit 23\n"
        "  fi\n"
        '  exec "$REAL_PG_DUMP" "$BACKUP_TEST_SOURCE_URL"\n'
        "fi\n"
        "exit 64\n",
    )
    return dump_marker


def _controlled_command_path(fake_bin: Path, *, omit: str) -> str:
    commands = {
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
    }
    for command in commands - {omit}:
        target = shutil.which(command)
        assert target is not None, command
        link = fake_bin / command
        if not link.exists():
            link.symlink_to(target)
    return str(fake_bin)


def _backup_environment(
    fake_bin: Path,
    backup_dir: Path,
    age_material: AgeMaterial,
    databases: DisposableDatabases,
) -> dict[str, str]:
    real_pg_dump = shutil.which("pg_dump")
    assert real_pg_dump is not None
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BACKUP_DIR": str(backup_dir),
        "BACKUP_AGE_RECIPIENT": age_material.recipient,
        "RETENTION_DAYS": "7",
        "POSTGRES_DB": databases.source_name,
        "BACKUP_TEST_SOURCE_URL": databases.source_url,
        "REAL_PG_DUMP": real_pg_dump,
        "PG_DUMP_MARKER": str(fake_bin / "pg_dump.called"),
    }


@pytest.mark.parametrize("missing", ["age", "age-keygen", "recipient"])
def test_encryption_preflight_fails_before_pg_dump(
    tmp_path: Path,
    age_material: AgeMaterial,
    missing: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = _install_fake_docker(fake_bin)
    environment = {
        **os.environ,
        "PATH": _controlled_command_path(fake_bin, omit=missing),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "BACKUP_AGE_RECIPIENT": age_material.recipient,
        "PG_DUMP_MARKER": str(marker),
    }
    if missing == "recipient":
        environment.pop("BACKUP_AGE_RECIPIENT")

    result = _run([str(BACKUP_SCRIPT)], environment=environment)

    assert result.returncode != 0
    assert not marker.exists()
    assert age_material.recipient not in result.stdout + result.stderr


def test_same_second_concurrent_backups_never_replace_recovery_point(
    tmp_path: Path,
    age_material: AgeMaterial,
    disposable_databases: DisposableDatabases,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_docker(fake_bin)
    fixed_timestamp = "20261007_212500"
    _write_executable(
        fake_bin / "date", f"#!/bin/sh\nprintf '%s\\n' '{fixed_timestamp}'\n"
    )
    backup_dir = tmp_path / "backups"
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    environment = _backup_environment(
        fake_bin, backup_dir, age_material, disposable_databases
    )
    environment["BACKUP_TEST_BARRIER_DIR"] = str(barrier_dir)

    processes = [
        subprocess.Popen(
            [str(BACKUP_SCRIPT)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    results = [
        process.communicate(timeout=30) + (process.returncode,) for process in processes
    ]

    assert sorted(result[2] for result in results) == [0, 1], results
    published = backup_dir / f"postgres_{fixed_timestamp}.sql.gz.age"
    assert published.is_file()
    preserved = published.read_bytes()
    assert preserved
    assert list(backup_dir.glob(".*.tmp.*")) == []
    assert list(backup_dir.glob("*.sql")) == []

    collision = _run([str(BACKUP_SCRIPT)], environment=environment)

    assert collision.returncode != 0
    assert published.read_bytes() == preserved
    assert list(backup_dir.glob(".*.tmp.*")) == []


def test_backup_restores_sentinel_without_plaintext_artifacts(
    tmp_path: Path,
    age_material: AgeMaterial,
    disposable_databases: DisposableDatabases,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = _install_fake_docker(fake_bin)
    backup_dir = tmp_path / "backups"
    environment = _backup_environment(
        fake_bin, backup_dir, age_material, disposable_databases
    )

    backup = _run([str(BACKUP_SCRIPT)], environment=environment)

    assert backup.returncode == 0, backup.stdout + backup.stderr
    assert marker.exists()
    published = list(backup_dir.glob("postgres_*.sql.gz.age"))
    assert len(published) == 1
    assert [item for item in backup_dir.iterdir() if item != published[0]] == []
    assert stat.S_IMODE(published[0].stat().st_mode) == 0o600
    assert age_material.recipient not in backup.stdout + backup.stderr

    restore_bin = tmp_path / "restore-bin"
    restore_bin.mkdir()
    real_psql = shutil.which("psql")
    assert real_psql is not None
    psql_arguments = tmp_path / "psql-arguments"
    psql_modes = tmp_path / "psql-pgpass-modes"
    url_leak = tmp_path / "restore-url-leaked"
    _write_executable(
        restore_bin / "psql",
        "#!/usr/bin/env bash\n"
        "set -u\n"
        'if [[ -n "${RESTORE_DATABASE_URL:-}" ]]; then : > "$URL_LEAK_MARKER"; exit 90; fi\n'
        "for variable in PGHOSTADDR PGPASSWORD PGSERVICE PGSERVICEFILE PGOPTIONS; do\n"
        '  if [[ -v "$variable" ]]; then : > "$URL_LEAK_MARKER"; exit 91; fi\n'
        "done\n"
        'stat -c "%a" "$PGPASSFILE" >> "$PSQL_MODE_LOG"\n'
        'printf "%s\\n" "$*" >> "$PSQL_ARGUMENT_LOG"\n'
        "input_file=$(mktemp)\n"
        'cat > "$input_file"\n'
        '"$REAL_PSQL" "$@" < "$input_file"\n'
        "status=$?\n"
        'rm -f -- "$input_file"\n'
        'exit "$status"\n',
    )
    credential_tmp = tmp_path / "restore-credentials"
    credential_tmp.mkdir()
    restore_environment = {
        **os.environ,
        "PATH": f"{restore_bin}:{os.environ['PATH']}",
        "BACKUP_AGE_IDENTITY": str(age_material.identity),
        "RESTORE_DATABASE_URL": disposable_databases.target_url,
        "RESTORE_EXPECTED_DATABASE": disposable_databases.target_url.rsplit("/", 1)[-1],
        "REAL_PSQL": real_psql,
        "PSQL_ARGUMENT_LOG": str(psql_arguments),
        "PSQL_MODE_LOG": str(psql_modes),
        "URL_LEAK_MARKER": str(url_leak),
        "TMPDIR": str(credential_tmp),
        "PGHOST": "hostile.invalid",
        "PGHOSTADDR": "203.0.113.50",
        "PGPORT": "1",
        "PGDATABASE": "hostile_database",
        "PGUSER": "hostile_user",
        "PGPASSWORD": "hostile-password",
        "PGPASSFILE": str(tmp_path / "hostile-pgpass"),
        "PGSERVICE": "hostile-service",
        "PGSERVICEFILE": str(tmp_path / "hostile-service.conf"),
        "PGOPTIONS": "-c search_path=hostile",
    }
    restore = _run(
        [str(RESTORE_SCRIPT), str(published[0])], environment=restore_environment
    )

    assert restore.returncode == 0, restore.stdout + restore.stderr
    assert str(age_material.identity) not in restore.stdout + restore.stderr
    assert disposable_databases.target_url not in restore.stdout + restore.stderr
    argument_log = psql_arguments.read_text(encoding="utf-8")
    password = make_url(disposable_databases.target_url).password
    assert "://" not in argument_log
    assert password is not None and password not in argument_log
    assert all("-X" in line.split() for line in argument_log.splitlines())
    assert not url_leak.exists()
    assert psql_modes.read_text(encoding="utf-8").splitlines() == ["600", "600"]
    assert list(credential_tmp.iterdir()) == []
    restored = _psql(
        disposable_databases.target_url,
        "SELECT value FROM backup_sentinel;\n",
    )
    assert restored.returncode == 0, restored.stderr
    assert restored.stdout.strip() == disposable_databases.sentinel


def test_restore_target_mismatch_stops_before_decryption(
    tmp_path: Path,
    age_material: AgeMaterial,
    disposable_databases: DisposableDatabases,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    decrypt_marker = tmp_path / "age-decrypt-called"
    _write_executable(
        fake_bin / "age",
        "#!/usr/bin/env bash\n" ': > "$DECRYPT_MARKER"\n' "exit 99\n",
    )
    backup_file = tmp_path / "dummy.sql.gz.age"
    backup_file.write_bytes(b"not reached")
    credential_tmp = tmp_path / "credentials"
    credential_tmp.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BACKUP_AGE_IDENTITY": str(age_material.identity),
        "RESTORE_DATABASE_URL": disposable_databases.target_url,
        "RESTORE_EXPECTED_DATABASE": disposable_databases.source_name,
        "DECRYPT_MARKER": str(decrypt_marker),
        "TMPDIR": str(credential_tmp),
    }

    result = _run([str(RESTORE_SCRIPT), str(backup_file)], environment=environment)

    assert result.returncode != 0
    assert "does not match" in result.stderr
    assert not decrypt_marker.exists()
    assert list(credential_tmp.iterdir()) == []


def test_restore_refuses_system_database_before_decryption(
    tmp_path: Path,
    age_material: AgeMaterial,
    postgres_admin_url: URL,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    decrypt_marker = tmp_path / "age-decrypt-called"
    _write_executable(
        fake_bin / "age",
        "#!/usr/bin/env bash\n" ': > "$DECRYPT_MARKER"\n' "exit 99\n",
    )
    backup_file = tmp_path / "dummy.sql.gz.age"
    backup_file.write_bytes(b"not reached")
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BACKUP_AGE_IDENTITY": str(age_material.identity),
        "RESTORE_DATABASE_URL": _render_url(postgres_admin_url),
        "RESTORE_EXPECTED_DATABASE": "postgres",
        "DECRYPT_MARKER": str(decrypt_marker),
    }

    result = _run([str(RESTORE_SCRIPT), str(backup_file)], environment=environment)

    assert result.returncode != 0
    assert "system database" in result.stderr
    assert not decrypt_marker.exists()


def test_restore_reads_option_like_identity_and_backup_paths_without_side_effects(
    tmp_path: Path,
    age_material: AgeMaterial,
    disposable_databases: DisposableDatabases,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_docker(fake_bin)
    backup_dir = tmp_path / "backups"
    backup = _run(
        [str(BACKUP_SCRIPT)],
        environment=_backup_environment(
            fake_bin, backup_dir, age_material, disposable_databases
        ),
    )
    assert backup.returncode == 0, backup.stdout + backup.stderr
    published = next(backup_dir.glob("postgres_*.sql.gz.age"))

    option_identity = tmp_path / "--output=unexpected-identity"
    option_backup = tmp_path / "--output=unexpected-backup"
    shutil.copyfile(age_material.identity, option_identity)
    option_identity.chmod(0o600)
    shutil.copyfile(published, option_backup)
    environment = {
        **os.environ,
        "BACKUP_AGE_IDENTITY": option_identity.name,
        "RESTORE_DATABASE_URL": disposable_databases.target_url,
        "RESTORE_EXPECTED_DATABASE": disposable_databases.target_url.rsplit("/", 1)[-1],
    }

    restore = subprocess.run(
        [str(RESTORE_SCRIPT), option_backup.name],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert restore.returncode == 0, restore.stdout + restore.stderr
    restored = _psql(
        disposable_databases.target_url,
        "SELECT value FROM backup_sentinel;\n",
    )
    assert restored.returncode == 0, restored.stderr
    assert restored.stdout.strip() == disposable_databases.sentinel
    assert not (tmp_path / "unexpected-identity").exists()
    assert not (tmp_path / "unexpected-backup").exists()


def test_malformed_restore_url_never_leaks_parser_secret(
    tmp_path: Path,
    age_material: AgeMaterial,
) -> None:
    credential_secret = "URL_CREDENTIAL_SECRET_8f3a"
    malformed_component = "INVALID_PORT_SECRET_71c9"
    backup_file = tmp_path / "dummy.sql.gz.age"
    backup_file.write_bytes(b"not reached")
    credential_tmp = tmp_path / "credentials"
    credential_tmp.mkdir()
    environment = {
        **os.environ,
        "BACKUP_AGE_IDENTITY": str(age_material.identity),
        "RESTORE_DATABASE_URL": (
            f"postgresql://restore:{credential_secret}@127.0.0.1:"
            f"{malformed_component}/recovery"
        ),
        "RESTORE_EXPECTED_DATABASE": "recovery",
        "TMPDIR": str(credential_tmp),
    }

    result = _run([str(RESTORE_SCRIPT), str(backup_file)], environment=environment)

    transcript = result.stdout + result.stderr
    assert result.returncode != 0
    assert transcript.strip().endswith("RESTORE_DATABASE_URL is invalid")
    assert credential_secret not in transcript
    assert malformed_component not in transcript
    assert "Traceback" not in transcript
    assert list(credential_tmp.iterdir()) == []


def test_signal_interrupt_cleans_temporary_and_skips_publish_and_retention(
    tmp_path: Path,
    age_material: AgeMaterial,
    disposable_databases: DisposableDatabases,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_docker(fake_bin)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    retained = backup_dir / "postgres_legacy.sql.gz.age"
    retained.write_bytes(b"existing recovery point")
    old = (datetime.now() - timedelta(days=30)).timestamp()
    os.utime(retained, (old, old))
    stall_marker = tmp_path / "pipeline-stalled"
    environment = _backup_environment(
        fake_bin, backup_dir, age_material, disposable_databases
    )
    environment["BACKUP_TEST_STALL_MARKER"] = str(stall_marker)

    process = subprocess.Popen(
        [str(BACKUP_SCRIPT)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not stall_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stall_marker.exists()
    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143, (stdout, stderr)
    assert retained.read_bytes() == b"existing recovery point"
    assert list(backup_dir.glob(".*.tmp.*")) == []
    assert "published" not in stdout
    assert "Removing encrypted PostgreSQL backups" not in stdout


@pytest.mark.parametrize("failing_stage", ["pg_dump", "gzip", "age"])
def test_pipeline_failure_publishes_nothing_and_cleans_temporary(
    tmp_path: Path,
    age_material: AgeMaterial,
    disposable_databases: DisposableDatabases,
    failing_stage: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_docker(fake_bin)
    backup_dir = tmp_path / "backups"
    environment = _backup_environment(
        fake_bin, backup_dir, age_material, disposable_databases
    )

    if failing_stage == "pg_dump":
        environment["FAIL_PG_DUMP"] = "1"
    elif failing_stage == "gzip":
        _write_executable(
            fake_bin / "gzip",
            "#!/usr/bin/env bash\nprintf 'partial compressed data'\nexit 31\n",
        )
    else:
        real_age = shutil.which("age")
        assert real_age is not None
        environment["REAL_AGE"] = real_age
        environment["AGE_CALL_COUNT"] = str(fake_bin / "age.calls")
        _write_executable(
            fake_bin / "age",
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            'count=0; [[ -f "$AGE_CALL_COUNT" ]] && count=$(wc -l < "$AGE_CALL_COUNT")\n'
            'printf "call\\n" >> "$AGE_CALL_COUNT"\n'
            'if [[ "$count" == "0" ]]; then exec "$REAL_AGE" "$@"; fi\n'
            "printf 'partial encrypted data'\n"
            "exit 32\n",
        )

    result = _run([str(BACKUP_SCRIPT)], environment=environment)

    assert result.returncode != 0
    assert backup_dir.exists()
    assert list(backup_dir.iterdir()) == []
    transcript = result.stdout + result.stderr
    assert age_material.recipient not in transcript
    assert disposable_databases.sentinel not in transcript


def test_retention_deletes_only_exact_top_level_encrypted_basename(
    tmp_path: Path,
    age_material: AgeMaterial,
    disposable_databases: DisposableDatabases,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_docker(fake_bin)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    eligible = backup_dir / "postgres_legacy.sql.gz.age"
    untouched = [
        backup_dir / "postgres_legacy.sql.gz",
        backup_dir / "postgres_data_legacy.tar.gz",
        backup_dir / "other.sql.gz.age",
        backup_dir / "prefix_postgres_legacy.sql.gz.age",
        backup_dir / "postgres_legacy.sql.gz.age.bak",
    ]
    eligible.write_bytes(b"legacy encrypted fixture")
    for artifact in untouched:
        artifact.write_bytes(b"legacy fixture")
    matching_directory = backup_dir / "postgres_directory.sql.gz.age"
    matching_directory.mkdir()
    nested = matching_directory / "postgres_nested.sql.gz.age"
    nested.write_bytes(b"nested fixture")
    old = (datetime.now() - timedelta(days=30)).timestamp()
    for path in [eligible, *untouched, matching_directory, nested]:
        os.utime(path, (old, old))

    environment = _backup_environment(
        fake_bin, backup_dir, age_material, disposable_databases
    )
    result = _run([str(BACKUP_SCRIPT)], environment=environment)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not eligible.exists()
    assert all(path.exists() for path in untouched)
    assert matching_directory.is_dir()
    assert nested.exists()
    assert len(list(backup_dir.glob("postgres_*.sql.gz.age"))) == 2
