"""The Group 7 fence has no HTTP path and starts unset after migration."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ROOT = Path(__file__).resolve().parents[2]
pytest_plugins = ("tests.integration.test_credit_orm_ddl_drift_postgres",)
pytestmark = pytest.mark.integration


async def test_enforcement_fence_is_server_disabled_and_unseeded(
    migrated: AsyncEngine,
) -> None:
    async with migrated.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM credit_enforcement_state")
            )
        ) == 0

    tree = ast.parse(
        (ROOT / "src/api/routes/admin_credits.py").read_text(encoding="utf-8")
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "arm_enforcement"
    ]
    assert not calls
