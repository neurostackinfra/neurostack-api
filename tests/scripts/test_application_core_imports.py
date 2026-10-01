"""Regression coverage for application imports of the extracted core package."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LEGACY_CONSUMERS = (
    Path("src/api/routes/policies.py"),
    Path("src/core/pipeline.py"),
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
APPROVED_SURFACES_BY_CONSUMER = {
    **{path: frozenset(FACADES) for path in LEGACY_CONSUMERS},
    Path("src/api/schemas/sessions.py"): frozenset({"neurostack_core.protocol"}),
}
REMOVED_SOURCE_PREFIXES = ("src.flow", "src.policy", "src.prompt", "src.srm")
ROOT_EXPORTED_SYMBOLS = frozenset(
    {
        "FLOW_DECISIONS_TOTAL",
        "FLOW_HANDLER_DURATION",
        "FLOW_TRANSITIONS_TOTAL",
        "OUTPUT_VALIDATIONS_TOTAL",
        "POLICY_EVALUATIONS",
        "POLICY_LATENCY",
        "POLICY_VIOLATIONS",
        "PROMPT_GENERATION_DURATION",
        "PROMPT_GENERATIONS_TOTAL",
        "PROMPT_TOKENS_TOTAL",
        "SRM_DECISIONS",
        "SRM_HANDOFFS",
        "SRM_LATENCY",
        "SRM_SAFETY_TRIGGERS",
        "Preset",
        "PresetError",
        "enable_metrics",
        "get_preset",
        "use_preset",
    }
)


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "neurostack_core":
                modules.extend(
                    f"neurostack_core.{alias.name}"
                    for alias in node.names
                    if alias.name not in ROOT_EXPORTED_SYMBOLS
                )
            else:
                modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return tuple(modules)


def _assert_approved_core_imports(
    relative_path: Path, modules: tuple[str, ...]
) -> None:
    core_imports = sorted(
        module for module in modules if module.startswith("neurostack_core.")
    )
    if not core_imports:
        return

    approved_surfaces = APPROVED_SURFACES_BY_CONSUMER.get(relative_path, frozenset())
    unapproved_imports = [
        module for module in core_imports if module not in approved_surfaces
    ]
    assert (
        not unapproved_imports
    ), f"{relative_path} imports unapproved core surfaces {unapproved_imports}"


def test_all_application_consumers_import_core_symbols_through_facades() -> None:
    actual_consumers: set[Path] = set()
    for path in sorted((ROOT / "src").rglob("*.py")):
        modules = _imports(path)
        relative_path = path.relative_to(ROOT)
        if any(module.startswith("neurostack_core.") for module in modules):
            actual_consumers.add(relative_path)
        _assert_approved_core_imports(relative_path, modules)

    assert actual_consumers == set(APPROVED_SURFACES_BY_CONSUMER)
    for relative_path in APPROVED_SURFACES_BY_CONSUMER:
        modules = _imports(ROOT / relative_path)
        assert any(
            module in APPROVED_SURFACES_BY_CONSUMER[relative_path] for module in modules
        )


def test_core_import_approvals_are_path_specific(tmp_path: Path) -> None:
    sessions = Path("src/api/schemas/sessions.py")
    legacy_consumer = LEGACY_CONSUMERS[0]

    def parsed(source: str) -> tuple[str, ...]:
        path = tmp_path / "consumer.py"
        path.write_text(source, encoding="utf-8")
        return _imports(path)

    protocol_root = parsed("from neurostack_core import protocol\n")
    assert protocol_root == ("neurostack_core.protocol",)
    _assert_approved_core_imports(sessions, protocol_root)

    with pytest.raises(AssertionError):
        _assert_approved_core_imports(
            sessions,
            parsed("from neurostack_core import protocol, srm\n"),
        )
    with pytest.raises(AssertionError):
        _assert_approved_core_imports(legacy_consumer, protocol_root)
    with pytest.raises(AssertionError):
        _assert_approved_core_imports(Path("src/services/unapproved.py"), protocol_root)
    with pytest.raises(AssertionError):
        _assert_approved_core_imports(
            legacy_consumer,
            parsed("from neurostack_core.flow.internal import private_symbol\n"),
        )

    assert parsed("from neurostack_core import enable_metrics\n") == ()


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
