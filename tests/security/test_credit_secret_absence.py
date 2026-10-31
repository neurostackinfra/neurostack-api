"""Group 7 observability/rollout code cannot introduce credit secret paths."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_rollout_has_no_secret_or_asaas_writer_dependency() -> None:
    path = ROOT / "src/services/credit_rollout.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "billing_rollout_control" not in names
    assert "topup_commercial_enabled" not in names
    assert "billing_topup_capability_receipts" not in names
    assert "secret_key" not in names
