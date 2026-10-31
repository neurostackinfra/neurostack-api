"""Group 7: credit readiness must not infer production from ENVIRONMENT."""

from __future__ import annotations

import ast
from pathlib import Path

from src.config.settings import Settings

ROOT = Path(__file__).resolve().parents[2]


def _credit_modules() -> list[Path]:
    paths = [
        *sorted((ROOT / "src/services").glob("credit_*.py")),
        ROOT / "src/services/content_retention.py",
        *sorted((ROOT / "src/models").glob("credit*.py")),
        ROOT / "src/models/content_retention.py",
        *sorted((ROOT / "src/repositories").glob("credit*.py")),
        *sorted((ROOT / "src/storage").glob("credit_*.py")),
        ROOT / "src/api/routes/credits.py",
        ROOT / "src/api/routes/admin_credits.py",
        ROOT / "src/api/schemas/credits.py",
        ROOT / "src/api/schemas/admin_credits.py",
    ]
    return [path for path in paths if path.exists()]


def test_credit_modules_never_read_is_production() -> None:
    offenders: list[str] = []
    for path in _credit_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "is_production":
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders


def test_credit_settings_do_not_require_appconfig() -> None:
    configured = Settings(
        environment="staging", secret_key="credit-role-test-key-0123456789abcdef"
    )
    assert configured.environment == "staging"
    assert configured.credit_binding_grace_days == 5
