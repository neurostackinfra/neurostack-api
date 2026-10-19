"""CI pin: the ``billing.fact.v1`` contract bytes are frozen.

The 2026-10-18 change "PAYMENT_CONFIRMED counts as paid" moved *when* a
settlement fact is produced, never *what* it contains: the schema, every
fixture and the manifest the bundle digest hashes must stay byte-identical,
so a consumer pinned to this bundle sees no difference.  A deliberate
contract change needs a new schema version, a new manifest and owner sign-off;
it must never edit these bytes in place.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.services.billing_outbox import (
    billing_fact_bundle_digest,
    billing_fact_schema_digest,
)

_ROOT = Path(__file__).parents[2]
_SCHEMA = "src/contracts/billing_fact_v1.schema.json"
_MANIFEST = "tests/fixtures/contracts/billing_fact_v1/SHA256SUMS"
_SCHEMA_SHA256 = "9d441e6c6a63a83a8955f981989ab5160da3466b8b2771a5cee1151647a5ff79"
_MANIFEST_SHA256 = "af630b3ea567ff68ddd4f6a0b3aae236b316d244a80ca9ac2c795c14051f4e77"


def _sha256(relative: str) -> str:
    return hashlib.sha256((_ROOT / relative).read_bytes()).hexdigest()


def test_fact_schema_and_bundle_manifest_are_byte_identical() -> None:
    assert _sha256(_SCHEMA) == _SCHEMA_SHA256
    assert _sha256(_MANIFEST) == _MANIFEST_SHA256
    # The producer's own digests are exactly these bytes.
    assert billing_fact_schema_digest() == _SCHEMA_SHA256
    assert billing_fact_bundle_digest() == _MANIFEST_SHA256


def test_every_manifest_entry_matches_its_file_and_nothing_is_unlisted() -> None:
    entries = {}
    for line in (_ROOT / _MANIFEST).read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        entries[relative] = digest
    for relative, digest in entries.items():
        assert _sha256(relative) == digest, relative
    listed = {path for path in entries if path.startswith("tests/fixtures/")}
    on_disk = {
        str(path.relative_to(_ROOT))
        for path in (_ROOT / "tests/fixtures/contracts/billing_fact_v1").glob("*.json")
    }
    assert on_disk == listed
    assert _SCHEMA in entries
