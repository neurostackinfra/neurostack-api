"""Executable boundary tests for the thin Gemini smoke wrapper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src.llm.gemini_smoke import RESULT_KEYS


def test_wrapper_emits_exact_canonical_failure_line() -> None:
    root = Path(__file__).resolve().parents[2]
    command = [
        sys.executable,
        str(root / "scripts/gemini_llm_smoke.py"),
        "--json",
        "--tested-commit",
        "a" * 40,
        "--image-digest",
        "registry.example/neurostack/api@sha256:" + "b" * 64,
        "--run-id",
        "run-42",
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(root),
            "GEMINI_MODEL": "model",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    artifact = json.loads(completed.stdout)
    assert set(artifact) == RESULT_KEYS
    assert artifact["result_code"] == "missing_api_key"
    assert (
        completed.stdout
        == json.dumps(
            artifact, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        + "\n"
    )
