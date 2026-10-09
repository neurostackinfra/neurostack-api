"""Subprocess contract for the mandatory handoff SLA PostgreSQL gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-handoff-sla-postgres.sh"
NODE = (
    "tests/integration/test_handoff_sla_postgres.py" "::test_handoff_sla_postgres_smoke"
)
CLONE_NODE = (
    "tests/integration/test_builder_cloning_postgres.py"
    "::test_builder_clone_omits_sla_target_and_preserves_runtime_snapshots"
)


def _run_gate(
    tmp_path: Path,
    *,
    transcript: str,
    pytest_status: int = 0,
    database_url: str | None = "postgresql:///postgres?host=/var/run/postgresql",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    poetry = fake_bin / "poetry"
    poetry.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" > "$ARGS_FILE"\n'
        'cat "$PYTEST_TRANSCRIPT"\n'
        'exit "$PYTEST_STATUS"\n',
        encoding="utf-8",
    )
    poetry.chmod(0o755)
    transcript_path = tmp_path / "pytest.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    args_path = tmp_path / "args.txt"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ARGS_FILE": str(args_path),
        "PYTEST_TRANSCRIPT": str(transcript_path),
        "PYTEST_STATUS": str(pytest_status),
    }
    if database_url is None:
        environment.pop("POSTGRES_SMOKE_DATABASE_URL", None)
    else:
        environment["POSTGRES_SMOKE_DATABASE_URL"] = database_url
    return (
        subprocess.run(
            [str(SCRIPT)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        ),
        args_path,
    )


def test_gate_runs_both_exact_nodes_and_accepts_two_passes(
    tmp_path: Path,
) -> None:
    result, args_path = _run_gate(
        tmp_path,
        transcript=".. [100%]\n2 passed in 0.42s\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert args_path.read_text(encoding="utf-8").strip() == (
        f"run pytest {NODE} {CLONE_NODE} -q -rs"
    )
    assert "2 passed, 0 skipped/xfail" in result.stdout


@pytest.mark.parametrize("database_url", [None, ""])
def test_gate_requires_a_nonblank_admin_url(
    tmp_path: Path,
    database_url: str | None,
) -> None:
    result, args_path = _run_gate(
        tmp_path,
        transcript="2 passed in 0.10s\n",
        database_url=database_url,
    )

    assert result.returncode == 2
    assert "POSTGRES_SMOKE_DATABASE_URL is required" in result.stderr
    assert not args_path.exists()


@pytest.mark.parametrize("pytest_status", [1, 2, 3, 4, 5])
def test_gate_rejects_every_nonzero_pytest_exit(
    tmp_path: Path,
    pytest_status: int,
) -> None:
    result, _ = _run_gate(
        tmp_path,
        transcript="2 passed in 0.10s\n",
        pytest_status=pytest_status,
    )

    assert result.returncode == 1
    assert f"smoke exited {pytest_status}" in result.stderr


@pytest.mark.parametrize(
    "transcript",
    [
        ". [100%]\n",
        "1 passed in 0.10s\n",
        "3 passed in 0.10s\n",
        "2 passed, 1 skipped in 0.10s\n",
        "2 passed, 1 xfailed in 0.10s\n",
        "2 passed, 1 xpassed in 0.10s\n",
        "2 passed, 1 failed in 0.10s\n",
        "2 passed, 1 error in 0.10s\n",
        "1 passed, 1 skipped in 0.10s\n",
        "1 passed, 1 xfailed in 0.10s\n",
        "1 passed, 1 xpassed in 0.10s\n",
        "1 passed, 1 failed in 0.10s\n",
        "1 passed, 1 error in 0.10s\n",
    ],
)
def test_gate_rejects_incomplete_or_forbidden_outcomes(
    tmp_path: Path,
    transcript: str,
) -> None:
    result, _ = _run_gate(tmp_path, transcript=transcript)

    assert result.returncode == 1
