"""Read the Alembic revision graph from the migration files without importing them.

The repository's ``alembic/`` directory shadows the installed ``alembic``
package when the project root is on ``sys.path``, so tests parse the scripts
with ``ast`` instead of using ``alembic.script.ScriptDirectory``.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"

RENUMBER_HINT = (
    "Renumber the newer migration to come after the current head: set its "
    "`revision` (and the third segment of its filename) to the next free ID "
    "and its `down_revision` to the current head. Merge migrations are not "
    "used in this repository."
)


def _module_string(path: Path, tree: ast.Module, name: str) -> str | None:
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if isinstance(value, ast.Tuple):
            raise AssertionError(
                f"{path.name}: `{name}` is a tuple (merge migration). {RENUMBER_HINT}"
            )
        if not isinstance(value, ast.Constant) or not (
            value.value is None or isinstance(value.value, str)
        ):
            raise AssertionError(f"{path.name}: `{name}` must be a string literal")
        return value.value
    raise AssertionError(f"{path.name}: module-level `{name}` not found")


def migration_revisions() -> dict[str, tuple[str, str | None]]:
    """Map migration filename -> (revision, down_revision)."""
    found: dict[str, tuple[str, str | None]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = _module_string(path, tree, "revision")
        if not revision:
            raise AssertionError(f"{path.name}: empty revision")
        found[path.name] = (revision, _module_string(path, tree, "down_revision"))
    return found


def heads(revisions: dict[str, tuple[str, str | None]]) -> list[str]:
    parents = Counter(down for _, down in revisions.values() if down is not None)
    return sorted({revision for revision, _ in revisions.values()} - set(parents))


def current_head() -> str:
    """Return the single head of the chain, failing loudly otherwise."""
    found = heads(migration_revisions())
    if len(found) != 1:
        raise AssertionError(
            f"expected exactly one head, found {found}. {RENUMBER_HINT}"
        )
    return found[0]
