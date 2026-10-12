"""Portable producer/consumer fixture compatibility proof."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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
