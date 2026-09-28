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


def _is_resource_import(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "importlib.resources"
        and len(node.names) == 1
        and node.names[0].name == "files"
        and node.names[0].asname is None
    )


def _is_resource_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "files"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and type(node.args[0].value) is str
        and node.args[0].value == "neurostack_core.presets"
        and not node.keywords
    )


def _direct_identifier_fields(node: ast.AST) -> tuple[tuple[str, str], ...]:
    """Return finite, source-visible identifier fields for one AST node."""

    values: tuple[tuple[str, str | None], ...] = ()
    if isinstance(node, ast.Name):
        values = (("id", node.id),)
    elif isinstance(node, ast.Attribute):
        values = (("attr", node.attr),)
    elif isinstance(node, ast.alias):
        values = (("name", node.name), ("asname", node.asname))
    elif isinstance(node, ast.ImportFrom):
        values = (("module", node.module),)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        values = (("name", node.name),)
    elif isinstance(node, ast.arg):
        values = (("arg", node.arg),)
    elif isinstance(node, ast.ExceptHandler):
        values = (("name", node.name),)
    elif isinstance(node, ast.keyword):
        values = (("arg", node.arg),)
    elif isinstance(node, (ast.Global, ast.Nonlocal)):
        values = tuple(("names", name) for name in node.names)
    elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
        values = (("name", node.name),)
    elif isinstance(node, ast.MatchMapping):
        values = (("rest", node.rest),)
    elif isinstance(node, ast.MatchClass):
        values = tuple(("kwd_attrs", name) for name in node.kwd_attrs)
    elif type(node).__name__ in {"TypeVar", "ParamSpec", "TypeVarTuple"}:
        values = (("name", getattr(node, "name", None)),)
    return tuple(
        (field, part)
        for field, value in values
        if value is not None
        for part in value.split(".")
    )


def _resource_syntax_exemption(tree: ast.AST) -> tuple[ast.ImportFrom, set[ast.Name]] | None:
    if not isinstance(tree, ast.Module):
        return None
    imports = [node for node in ast.walk(tree) if _is_resource_import(node)]
    if len(imports) != 1 or imports[0] not in tree.body:
        return None
    calls = [node for node in ast.walk(tree) if _is_resource_call(node)]
    if not calls:
        return None
    resource_import = imports[0]
    allowed_names = {call.func for call in calls}
    allowed_alias = resource_import.names[0]
    for node in ast.walk(tree):
        for field, identifier in _direct_identifier_fields(node):
            if identifier != "files":
                continue
            if node is allowed_alias and field == "name":
                continue
            if node in allowed_names and field == "id":
                continue
            return None
    return resource_import, allowed_names


def find_forbidden_imports(
    tree: ast.AST, *, allow_packaged_resources: bool = False
) -> list[ForbiddenImport]:
    """Find forbidden occurrences with one finite structural AST walk."""

    occurrences: set[ForbiddenImport] = set()
    exemption = _resource_syntax_exemption(tree) if allow_packaged_resources else None
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
        spellings = reserved_spellings(node)
        if exemption is not None and node is exemption[0]:
            spellings = tuple(spelling for spelling in spellings if spelling != "importlib")
        for spelling in spellings:
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
