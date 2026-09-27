"""Classify source-visible package-boundary import syntax."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Literal

Category = Literal["static-src-import", "reserved-import-syntax"]

RESERVED_SPELLINGS = frozenset(
    {"importlib", "builtins", "__builtins__", "import_module", "__import__"}
)


@dataclass(frozen=True)
class ForbiddenImport:
    """One forbidden syntax occurrence, independent of runtime semantics."""

    category: Category
    lineno: int
    col_offset: int
    spelling: str


def _identifier_parts(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(part for part in value.split(".") if part in RESERVED_SPELLINGS)


def reserved_spellings(node: ast.AST) -> tuple[str, ...]:
    """Return reserved identifier spellings directly exposed by one syntax node."""

    values: tuple[str | None, ...] = ()
    if isinstance(node, ast.Name):
        values = (node.id,)
    elif isinstance(node, ast.Attribute):
        values = (node.attr,)
    elif isinstance(node, ast.alias):
        values = (node.name, node.asname)
    elif isinstance(node, ast.ImportFrom):
        values = (node.module,)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        values = (node.name,)
    elif isinstance(node, ast.arg):
        values = (node.arg,)
    elif isinstance(node, ast.ExceptHandler):
        values = (node.name,)
    elif isinstance(node, ast.keyword):
        values = (node.arg,)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        values = tuple(node.names)
    elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
        values = (node.name,)
    elif isinstance(node, ast.MatchMapping):
        values = (node.rest,)
    elif isinstance(node, ast.MatchClass):
        values = tuple(node.kwd_attrs)
    return tuple(part for value in values for part in _identifier_parts(value))


def _is_src_module(module: str | None) -> bool:
    return module == "src" or bool(module and module.startswith("src."))


def find_forbidden_imports(tree: ast.AST) -> list[ForbiddenImport]:
    """Find forbidden occurrences with one finite structural AST walk."""

    occurrences: set[ForbiddenImport] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_src_module(alias.name):
                    occurrences.add(
                        ForbiddenImport(
                            "static-src-import",
                            node.lineno,
                            node.col_offset,
                            alias.name,
                        )
                    )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _is_src_module(node.module)
        ):
            occurrences.add(
                ForbiddenImport(
                    "static-src-import",
                    node.lineno,
                    node.col_offset,
                    node.module or "src",
                )
            )
        for spelling in reserved_spellings(node):
            occurrences.add(
                ForbiddenImport(
                    "reserved-import-syntax",
                    getattr(node, "lineno", 0),
                    getattr(node, "col_offset", 0),
                    spelling,
                )
            )
    return sorted(
        occurrences,
        key=lambda item: (
            item.lineno,
            item.col_offset,
            item.category,
            item.spelling,
        ),
    )
