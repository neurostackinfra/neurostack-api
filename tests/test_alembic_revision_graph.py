"""Structural guard for the Alembic revision graph.

Revision IDs are short sequential strings ("022", "023"), so two branches that
each add "the next" migration collide silently: Alembic keeps one of the
duplicates and ``upgrade head`` breaks on whichever database applied the other.
These checks make that collision fail in the test suite of the branch that
introduces it, before it reaches main.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"

RENUMBER_HINT = (
    "Renumber the newer migration to come after the current head: set its "
    "`revision` (and the third segment of its filename) to the next free ID "
    "and its `down_revision` to the current head."
)


def _module_string(tree: ast.Module, name: str) -> str | None:
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            assert isinstance(value, ast.Constant), f"{name} must be a literal"
            assert value.value is None or isinstance(value.value, str)
            return value.value
    raise AssertionError(f"module-level `{name}` not found")


def _revisions() -> dict[str, tuple[str, str | None]]:
    """Map migration filename -> (revision, down_revision)."""
    found: dict[str, tuple[str, str | None]] = {}
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = _module_string(tree, "revision")
        assert revision, f"{path.name}: empty revision"
        found[path.name] = (revision, _module_string(tree, "down_revision"))
    return found


def test_revision_ids_are_unique() -> None:
    counts = Counter(revision for revision, _ in _revisions().values())
    duplicated = {
        revision: sorted(
            name for name, (rev, _) in _revisions().items() if rev == revision
        )
        for revision, count in counts.items()
        if count > 1
    }
    assert not duplicated, f"duplicate revision IDs {duplicated}. {RENUMBER_HINT}"


def test_filename_carries_the_revision_id() -> None:
    mismatched = {
        name: revision
        for name, (revision, _) in _revisions().items()
        if name.split("_")[2] != revision
    }
    assert not mismatched, (
        f"filename segment does not match `revision`: {mismatched}. "
        "Name migrations YYYYMMDD_HHMMSS_<revision>_<slug>.py."
    )


def test_graph_is_one_linear_chain() -> None:
    revisions = _revisions()
    known = {revision for revision, _ in revisions.values()}
    roots = sorted(name for name, (_, down) in revisions.items() if down is None)
    dangling = {
        name: down
        for name, (_, down) in revisions.items()
        if down is not None and down not in known
    }
    parents = Counter(down for _, down in revisions.values() if down is not None)
    branch_points = sorted(down for down, count in parents.items() if count > 1)
    heads = sorted(known - set(parents))

    assert roots == ["20251231_000000_001_initial_schema.py"], f"roots: {roots}"
    assert not dangling, f"down_revision points to an unknown revision: {dangling}"
    assert (
        not branch_points
    ), f"more than one migration revises {branch_points}. {RENUMBER_HINT}"
    assert len(heads) == 1, f"expected exactly one head, found {heads}. {RENUMBER_HINT}"
