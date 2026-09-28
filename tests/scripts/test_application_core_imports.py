"""Regression coverage for application imports of the extracted core package."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONSUMERS = (
    Path("src/api/routes/policies.py"),
    Path("src/core/pipeline.py"),
    Path("src/handoff/sla.py"),
    Path("src/memory/__init__.py"),
    Path("src/memory/models.py"),
    Path("src/services/business_hours.py"),
    Path("src/services/trace_adapter.py"),
)
FACADES = {
    "neurostack_core.flow",
    "neurostack_core.policy",
    "neurostack_core.prompt",
    "neurostack_core.srm",
}
REMOVED_SOURCE_PREFIXES = ("src.flow", "src.policy", "src.prompt", "src.srm")


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return tuple(modules)


def test_all_application_consumers_import_core_symbols_through_facades() -> None:
    actual_consumers: set[Path] = set()
    for path in sorted((ROOT / "src").rglob("*.py")):
        modules = _imports(path)
        if any(
            module in FACADES or module.startswith("neurostack_core.")
            for module in modules
        ):
            actual_consumers.add(path.relative_to(ROOT))
        deep_imports = sorted(
            module
            for module in modules
            if module.startswith(tuple(f"{facade}." for facade in FACADES))
        )
        assert not deep_imports, f"{path.relative_to(ROOT)} deep-imports {deep_imports}"

    assert actual_consumers == set(CONSUMERS)
    for relative_path in CONSUMERS:
        modules = _imports(ROOT / relative_path)
        assert any(module in FACADES for module in modules)


def test_removed_core_source_paths_are_not_referenced() -> None:
    audited = (
        ROOT / "tests" / "prompt" / "test_timezone_greeting.py",
        ROOT / "src" / "repositories" / "INTEGRATION.md",
    )
    for path in audited:
        source = path.read_text(encoding="utf-8")
        stale = [prefix for prefix in REMOVED_SOURCE_PREFIXES if prefix in source]
        stale.extend(
            prefix.replace(".", "/")
            for prefix in REMOVED_SOURCE_PREFIXES
            if prefix.replace(".", "/") in source
        )
        assert not stale, f"{path.relative_to(ROOT)} references removed paths: {stale}"
