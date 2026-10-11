"""Structural guard for the Alembic revision graph.

Revision IDs are short sequential strings ("022", "023"), so two branches that
each add "the next" migration collide silently: Alembic keeps one of the
duplicates and ``upgrade head`` breaks on whichever database applied the other.

The files usually have different names, so git reports no conflict and each
branch alone still forms a valid chain. These checks only fire once both
migrations are in the same tree: bring the branch up to date with ``main`` and
run this suite before merging.
"""

from __future__ import annotations

from collections import Counter

from tests.alembic_graph import RENUMBER_HINT, heads, migration_revisions


def test_revision_ids_are_unique() -> None:
    revisions = migration_revisions()
    counts = Counter(revision for revision, _ in revisions.values())
    duplicated = {
        revision: sorted(
            name for name, (rev, _) in revisions.items() if rev == revision
        )
        for revision, count in counts.items()
        if count > 1
    }
    assert not duplicated, f"duplicate revision IDs {duplicated}. {RENUMBER_HINT}"


def test_filename_carries_the_revision_id() -> None:
    malformed = []
    mismatched = {}
    for name, (revision, _) in migration_revisions().items():
        parts = name.split("_", 3)
        if len(parts) < 4:
            malformed.append(name)
        elif parts[2] != revision:
            mismatched[name] = revision
    assert (
        not malformed
    ), f"filenames outside YYYYMMDD_HHMMSS_<revision>_<slug>.py: {malformed}"
    assert not mismatched, (
        f"filename segment does not match `revision`: {mismatched}. "
        "Name migrations YYYYMMDD_HHMMSS_<revision>_<slug>.py."
    )


def test_graph_is_one_linear_chain() -> None:
    revisions = migration_revisions()
    known = {revision for revision, _ in revisions.values()}
    roots = sorted(name for name, (_, down) in revisions.items() if down is None)
    dangling = {
        name: down
        for name, (_, down) in revisions.items()
        if down is not None and down not in known
    }
    parents = Counter(down for _, down in revisions.values() if down is not None)
    branch_points = sorted(down for down, count in parents.items() if count > 1)
    found_heads = heads(revisions)

    assert roots == ["20251231_000000_001_initial_schema.py"], f"roots: {roots}"
    assert not dangling, f"down_revision points to an unknown revision: {dangling}"
    assert (
        not branch_points
    ), f"more than one migration revises {branch_points}. {RENUMBER_HINT}"
    assert (
        len(found_heads) == 1
    ), f"expected exactly one head, found {found_heads}. {RENUMBER_HINT}"
