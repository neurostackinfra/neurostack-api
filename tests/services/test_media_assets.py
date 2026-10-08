"""Unit contracts for media validation and storage-owner keys."""

from __future__ import annotations

from uuid import UUID

import pytest

from src.services import media_assets
from src.services.media_assets import (
    MediaAssetDomainError,
    classify_media_payload,
    media_payload_sha256,
    media_storage_advisory_key,
    parse_auto_suggest_wire,
    read_media_payload_limited,
    validate_media_classification,
    validate_media_metadata,
)


class _ByteStream:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    async def read(self, size: int = -1) -> bytes:
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else self.offset + size
        chunk = self.payload[self.offset : end]
        self.offset += len(chunk)
        return chunk


@pytest.mark.parametrize(
    ("owner_id", "expected"),
    [
        ("00000000-0000-0000-0000-000000000000", -4516021718240404535),
        ("ffffffff-ffff-ffff-ffff-ffffffffffff", -5828949329638390497),
        ("12345678-1234-5678-9abc-def012345678", 5896964795730298223),
    ],
)
def test_advisory_key_has_fixed_sha256_bigint_vectors(
    owner_id: str,
    expected: int,
) -> None:
    assert media_storage_advisory_key(UUID(owner_id)) == expected


def test_metadata_preserves_exact_values_and_defaults_auto_suggest() -> None:
    metadata = validate_media_metadata(
        display_name="  Nome autoral  ",
        original_filename=" mídia final.png ",
        usage_trigger="quando a pessoa pedir",
    )
    assert metadata.display_name == "  Nome autoral  "
    assert metadata.original_filename == " mídia final.png "
    assert metadata.usage_trigger == "quando a pessoa pedir"
    assert metadata.auto_suggest is False


@pytest.mark.parametrize(
    ("wire", "expected"),
    [(b"true", True), ("true", True), (b"false", False), ("false", False)],
)
def test_auto_suggest_accepts_only_lowercase_ascii(
    wire: bytes | str,
    expected: bool,
) -> None:
    assert parse_auto_suggest_wire(wire) is expected


@pytest.mark.parametrize(
    "wire",
    [b"TRUE", "False", b"1", "0", b"", "", True, False, b" true"],
)
def test_auto_suggest_rejects_every_other_wire_value(wire: object) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_metadata_invalid"):
        parse_auto_suggest_wire(wire)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"display_name": ""},
        {"display_name": "x" * 201},
        {"original_filename": ""},
        {"original_filename": "x" * 256},
        {"usage_trigger": "x" * 501},
        {"display_name": "nul\x00name"},
        {"original_filename": "line\rbreak"},
        {"usage_trigger": "line\nbreak"},
    ],
)
def test_metadata_rejects_lengths_and_control_characters(
    overrides: dict[str, str],
) -> None:
    values = {
        "display_name": "asset",
        "original_filename": "asset.pdf",
        "usage_trigger": None,
    }
    values.update(overrides)
    with pytest.raises(MediaAssetDomainError, match="media_metadata_invalid"):
        validate_media_metadata(**values)  # type: ignore[arg-type]


def test_metadata_accepts_exact_length_boundaries() -> None:
    metadata = validate_media_metadata(
        display_name="d" * 200,
        original_filename="f" * 255,
        usage_trigger="u" * 500,
        auto_suggest=b"true",
    )
    assert len(metadata.display_name) == 200
    assert len(metadata.original_filename) == 255
    assert len(metadata.usage_trigger or "") == 500
    assert metadata.auto_suggest is True


@pytest.mark.parametrize(
    ("payload", "category", "mime_type"),
    [
        (b"%PDF-1.7\nbody", "pdf", "application/pdf"),
        (b"\x89PNG\r\n\x1a\nbody", "image", "image/png"),
        (b"\xff\xd8\xff\xe0jpeg", "image", "image/jpeg"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x64", "audio", "audio/mpeg"),
        (b"\xff\xfb\x90\x64mp3", "audio", "audio/mpeg"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42", "video", "video/mp4"),
    ],
)
def test_classification_derives_each_canonical_pair(
    payload: bytes,
    category: str,
    mime_type: str,
) -> None:
    classification = classify_media_payload(payload)
    assert (classification.category, classification.mime_type) == (
        category,
        mime_type,
    )
    assert validate_media_classification(payload, mime_type) == classification
    assert len(media_payload_sha256(payload)) == 32


def test_classification_rejects_unknown_signature_unsupported_and_mismatch() -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(b"not-media")
    with pytest.raises(MediaAssetDomainError, match="media_type_unsupported"):
        validate_media_classification(b"%PDF-1.7", "text/plain")
    with pytest.raises(MediaAssetDomainError, match="media_type_mismatch"):
        validate_media_classification(b"%PDF-1.7", "image/png")


@pytest.mark.parametrize(
    "payload",
    [
        b"ID3",
        b"ID3\x04\x00\x00\x00\x00\x00\x00",
        b"ID3\x04\x00\x00\x00\x00\x00\x05short",
        b"ID3\x04\x00\x00\x80\x00\x00\x00\xff\xfb\x90\x64",
        b"\xff\xff\x90\x64layer-i",
        b"\xff\xfd\x90\x64layer-ii",
        b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xff\x90\x64",
        b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfd\x90\x64",
        b"\x00\x00\x00\x0cftypisom",
        b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00heicmif1",
        b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1",
        b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom",
        b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ",
        b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomheic",
    ],
)
def test_classification_rejects_truncated_or_non_mp4_container_signatures(
    payload: bytes,
) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(payload)


@pytest.mark.asyncio
async def test_bounded_reader_stops_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_assets, "MEDIA_FILE_MAX_BYTES", 4)
    monkeypatch.setattr(media_assets, "MEDIA_READ_CHUNK_BYTES", 2)
    assert await read_media_payload_limited(_ByteStream(b"1234")) == b"1234"
    with pytest.raises(MediaAssetDomainError, match="media_size_exceeded"):
        await read_media_payload_limited(_ByteStream(b"12345"))
