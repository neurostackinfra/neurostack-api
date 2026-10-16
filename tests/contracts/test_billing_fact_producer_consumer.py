"""Portable producer/consumer fixture compatibility proof."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]


def test_canonical_bundle_validates_without_starting_the_application() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify-billing-fact-bundle.py",
            "--schema",
            "src/contracts/billing_fact_v1.schema.json",
            "--fixtures",
            "tests/fixtures/contracts/billing_fact_v1",
            "--manifest",
            "tests/fixtures/contracts/billing_fact_v1/SHA256SUMS",
            "--emit-digest",
        ],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip()) == 64


def test_bundle_verifier_rejects_weakened_paid_at_schema() -> None:
    verify_schema = runpy.run_path(
        str(_ROOT / "scripts/verify-billing-fact-bundle.py")
    )["_verify_schema"]
    schema = json.loads(
        (_ROOT / "src/contracts/billing_fact_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    schema["properties"]["paid_at"] = {"type": ["string", "null"]}
    with pytest.raises(ValueError, match="paid_at property"):
        verify_schema(schema)

    schema = json.loads(
        (_ROOT / "src/contracts/billing_fact_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    schema["allOf"] = []
    with pytest.raises(ValueError, match="paid_at settlement condition"):
        verify_schema(schema)
