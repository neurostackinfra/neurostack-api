"""Keep ASAAS producer migrations on the current mainline revision chain."""

from __future__ import annotations

from tests.alembic_graph import heads, migration_revisions


def test_asaas_producer_migrations_extend_026_through_037_linearly() -> None:
    revisions = migration_revisions()
    expected = {
        "026": "025",
        "027": "026",
        "028": "027",
        "029": "028",
        "030": "029",
        "031": "030",
        "032": "031",
        "033": "032",
        "034": "033",
        "035": "034",
        # Group 10 E5 (decouple from the legacy roster).
        "036": "035",
        # Group 10 E6 (drop the legacy migration saga and roster objects).
        "037": "036",
    }
    by_revision: dict[str, list[tuple[str, str | None]]] = {}
    for filename, (revision, down_revision) in revisions.items():
        by_revision.setdefault(revision, []).append((filename, down_revision))

    for revision, expected_parent in expected.items():
        matches = by_revision.get(revision, [])
        assert len(matches) == 1, f"revision {revision} must occur once: {matches}"
        filename, down_revision = matches[0]
        assert down_revision == expected_parent, (
            f"revision {revision} must revise {expected_parent}, "
            f"found {down_revision} in {filename}"
        )
        assert filename.split("_", 3)[2] == revision

    # neurostack-credit-system Group 1a: the additive credit schema (038, not
    # an ASAAS producer migration) follows 037; Group 4a's credit inventory
    # (039) follows 038 and is the head.
    assert heads(revisions) == ["039"]
