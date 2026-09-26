#!/usr/bin/env python3
"""Extract the minimal core API used by consumers in an archived source tree."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Iterable

from _forbidden_imports import reserved_spellings

CONSUMERS = (
    "src/api/routes/policies.py",
    "src/core/pipeline.py",
    "src/memory/__init__.py",
    "src/memory/models.py",
    "src/services/business_hours.py",
    "src/services/trace_adapter.py",
)
CORE_PREFIXES = ("src.flow", "src.policy", "src.prompt", "src.srm")


class ExtractionError(ValueError):
    """The archived source cannot provide a deterministic public API."""


def module_path(root: Path, module: str) -> Path:
    relative = Path(*module.split("."))
    package = root / relative / "__init__.py"
    source = root / relative.with_suffix(".py")
    if package.is_file():
        return package
    if source.is_file():
        return source
    raise ExtractionError(f"unresolved module: {module}")


def literal_all(tree: ast.Module, module: str) -> set[str]:
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in targets
            ):
                continue
            if node.value is None:
                raise ExtractionError(f"dynamic __all__ in {module}")
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise ExtractionError(f"dynamic __all__ in {module}") from exc
            if not isinstance(value, (list, tuple)) or not all(
                isinstance(item, str) for item in value
            ):
                raise ExtractionError(f"invalid __all__ in {module}")
            return set(value)
    raise ExtractionError(f"missing __all__ in facade {module}")


def defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(assigned_names(target))
        elif isinstance(node, ast.AnnAssign):
            names.update(assigned_names(node.target))
    return names


def assigned_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for child in node.elts for name in assigned_names(child)}
    return set()


def is_core_module(module: str) -> bool:
    return any(
        module == prefix or module.startswith(prefix + ".") for prefix in CORE_PREFIXES
    )


def public_module(module: str) -> str:
    domain = module.split(".", 2)[1]
    return f"neurostack_core.{domain}"


def attribute_parts(node: ast.Attribute) -> list[str] | None:
    parts = [node.attr]
    value: ast.expr = node.value
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if not isinstance(value, ast.Name):
        return None
    parts.append(value.id)
    return list(reversed(parts))


def prove_symbol(root: Path, module: str, symbol: str, facade: bool) -> None:
    path = module_path(root, module)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise ExtractionError(f"invalid Python in {module}: {exc}") from exc
    available = literal_all(tree, module) if facade else defined_names(tree)
    if symbol not in available:
        kind = "facade export" if facade else "definition"
        raise ExtractionError(f"missing {kind}: {module}.{symbol}")


def extract_tree(root: Path, tree: ast.Module, source_name: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    module_aliases: dict[str, list[tuple[str, int]]] = {}
    rebindings: dict[str, list[int]] = {}
    attributes: list[ast.Attribute] = []

    def bind(name: str, lineno: int) -> None:
        rebindings.setdefault(name, []).append(lineno)

    class ExtractionVisitor(ast.NodeVisitor):
        """Collect static facade syntax without evaluating Python control flow."""

        def visit(self, node: ast.AST) -> None:
            spellings = reserved_spellings(node)
            if spellings:
                raise ExtractionError(
                    f"reserved import syntax {spellings[0]!r} in "
                    f"{source_name}:{getattr(node, 'lineno', 0)}"
                )
            if isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Del)
            ):
                bind(node.id, node.lineno)
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                bind(node.name, node.lineno)
            elif isinstance(node, ast.arg):
                bind(node.arg, node.lineno)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, node.lineno)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                bind(node.name, node.lineno)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                bind(node.rest, node.lineno)
            super().visit(node)

        def visit_If(self, node: ast.If) -> None:
            self.visit(node.test)
            statements = (
                node.orelse
                if is_type_checking_guard(node.test)
                else node.body + node.orelse
            )
            for statement in statements:
                self.visit(statement)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                if is_core_module(alias.name):
                    imported = alias.name if alias.asname else alias.name.split(".")[0]
                    module_aliases.setdefault(local, []).append((imported, node.lineno))
                else:
                    bind(local, node.lineno)
            self.generic_visit(node)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            if node.level or node.module is None or not is_core_module(node.module):
                for alias in node.names:
                    if alias.name != "*":
                        bind(alias.asname or alias.name, node.lineno)
                self.generic_visit(node)
                return
            for alias in node.names:
                if alias.name == "*":
                    raise ExtractionError(f"star import in {source_name}:{node.lineno}")
                prove_symbol(
                    root, node.module, alias.name, node.module in CORE_PREFIXES
                )
                result.setdefault(public_module(node.module), set()).add(alias.name)
                bind(alias.asname or alias.name, node.lineno)
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            attributes.append(node)
            self.generic_visit(node)

    ExtractionVisitor().visit(tree)

    aliases: dict[str, str] = {}
    for local, definitions in sorted(module_aliases.items()):
        modules = {module for module, _ in definitions}
        if len(modules) != 1:
            lines = ", ".join(str(line) for _, line in definitions)
            raise ExtractionError(
                f"conflicting facade alias {local!r} in {source_name}:{lines}"
            )
        if local in rebindings:
            lines = ", ".join(str(line) for line in rebindings[local])
            raise ExtractionError(
                f"rebound facade alias {local!r} in {source_name}:{lines}"
            )
        aliases[local] = next(iter(modules))

    for node in attributes:
        parts = attribute_parts(node)
        if parts is None or parts[0] not in aliases:
            continue
        resolved = ".".join((aliases[parts[0]], *parts[1:]))
        try:
            module_path(root, resolved)
        except ExtractionError:
            pass
        else:
            continue
        resolved_parts = resolved.split(".")
        if len(resolved_parts) < 3:
            continue
        module = ".".join(resolved_parts[:-1])
        if not is_core_module(module):
            continue
        try:
            module_path(root, module)
        except ExtractionError:
            continue
        symbol = resolved_parts[-1]
        prove_symbol(root, module, symbol, module in CORE_PREFIXES)
        result.setdefault(public_module(module), set()).add(symbol)
    return result


def is_type_checking_guard(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "TYPE_CHECKING"
    return isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"


def merge(target: dict[str, set[str]], source: dict[str, set[str]]) -> None:
    for module, names in source.items():
        target.setdefault(module, set()).update(names)


def read_quick_start_trees(readme: Path) -> Iterable[tuple[ast.Module, str]]:
    lines = readme.read_text(encoding="utf-8").splitlines()
    in_quick_start = False
    in_python = False
    block: list[str] = []
    start = 0
    for lineno, line in enumerate(lines, 1):
        if line.startswith("## "):
            if in_quick_start:
                break
            in_quick_start = "Quick Start" in line
            continue
        if not in_quick_start:
            continue
        if line.startswith("```"):
            language = line[3:].strip().lower()
            if not in_python:
                in_python = language in {"py", "python", "python3"}
                block = []
                start = lineno + 1
            else:
                source = "\n".join(block) + "\n"
                try:
                    yield ast.parse(
                        source, filename=f"README.md:{start}"
                    ), f"README.md:{start}"
                except SyntaxError as exc:
                    raise ExtractionError(f"invalid Quick Start Python: {exc}") from exc
                in_python = False
            continue
        if in_python:
            block.append(line)
    if in_python:
        raise ExtractionError("unterminated Python fence in README Quick Start")


def extract(root: Path) -> dict[str, list[str]]:
    if not root.is_dir():
        raise ExtractionError(f"archive tree is missing: {root}")
    result: dict[str, set[str]] = {}
    for relative in CONSUMERS:
        path = root / relative
        if not path.is_file():
            raise ExtractionError(f"missing consumer: {relative}")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        merge(result, extract_tree(root, tree, relative))
    readme = root / "README.md"
    if not readme.is_file():
        raise ExtractionError("missing README.md")
    for tree, source_name in read_quick_start_trees(readme):
        merge(result, extract_tree(root, tree, source_name))
    return {module: sorted(names) for module, names in sorted(result.items())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = extract(args.tree.resolve())
    except ExtractionError as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
