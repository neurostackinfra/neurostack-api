"""Unit contracts for media validation, storage-owner keys and the capacity guard."""

from __future__ import annotations

import random
import tracemalloc
from collections.abc import Iterator
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.services import media_assets
from src.services.media_assets import (
    MEDIA_AUDIO_VIDEO_MAX_BYTES,
    MEDIA_CANONICAL_MIME_TYPES,
    MEDIA_CATEGORY_MAX_BYTES,
    MEDIA_FILE_EXTENSIONS,
    MEDIA_IMAGE_MAX_BYTES,
    MEDIA_PDF_MAX_BYTES,
    MEDIA_STORAGE_CAP_BYTES,
    MediaAssetDomainError,
    MediaStorageCapacityExceeded,
    MediaStorageCapacityGuard,
    MediaStorageGuardError,
    MediaStorageProjection,
    canonical_declared_mime,
    classify_media_payload,
    media_payload_sha256,
    media_storage_advisory_key,
    ogg_opus_channel_count,
    ogg_opus_duration_seconds,
    ogg_opus_identification_header,
    parse_auto_suggest_wire,
    read_media_payload_limited,
    validate_media_classification,
    validate_media_metadata,
    validate_trigger_intents,
)
from tests.ogg_opus_audio import ogg_opus_file, ogg_page, opus_identification_header


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


def _opus_head(version: int = 1, channels: int = 1) -> bytes:
    """The 19-byte RFC 7845 identification header."""

    return (
        b"OpusHead"
        + bytes([version, channels])
        + (312).to_bytes(2, "little")  # pre-skip
        + (48_000).to_bytes(4, "little")  # input sample rate
        + (0).to_bytes(2, "little")  # output gain
        + bytes([0])  # channel mapping family
    )


def _ogg_page(
    *,
    capture: bytes = b"OggS",
    version: int = 0,
    header_type: int = 0x02,
    segments: bytes = b"\x13",
    segment_count: int | None = None,
    body: bytes | None = None,
) -> bytes:
    """One RFC 3533 page; the CRC is never read, so it stays zero."""

    return (
        capture
        + bytes([version, header_type])
        + bytes(8)  # granule position
        + bytes(4)  # bitstream serial number
        + bytes(4)  # page sequence number
        + bytes(4)  # CRC
        + bytes([len(segments) if segment_count is None else segment_count])
        + segments
        + (_opus_head() if body is None else body)
    )


def _box(box_type: bytes, body: bytes = b"") -> bytes:
    """One ISO-BMFF box with a 32-bit size."""

    return (8 + len(body)).to_bytes(4, "big") + box_type + body


def _large_box(box_type: bytes, body: bytes = b"") -> bytes:
    """Size 1: the real size follows the type as a 64-bit ``largesize``."""

    return (
        (1).to_bytes(4, "big") + box_type + (16 + len(body)).to_bytes(8, "big") + body
    )


def _to_end_box(box_type: bytes, body: bytes = b"") -> bytes:
    """Size 0: the box runs to the end of what contains it."""

    return bytes(4) + box_type + body


def _ftyp(major: bytes, *compatible: bytes) -> bytes:
    return _box(b"ftyp", major + bytes(4) + b"".join(compatible))


def _hdlr(handler: bytes) -> bytes:
    # FullBox version and flags, pre_defined, handler_type, reserved, name.
    return _box(b"hdlr", bytes(4) + bytes(4) + handler + bytes(12) + b"\x00")


def _trak(handler: bytes) -> bytes:
    mdia = _box(b"mdhd", bytes(24)) + _hdlr(handler) + _box(b"minf")
    return _box(b"trak", _box(b"tkhd", bytes(84)) + _box(b"mdia", mdia))


def _moov(*handlers: bytes) -> bytes:
    tracks = b"".join(_trak(handler) for handler in handlers)
    return _box(b"moov", _box(b"mvhd", bytes(100)) + tracks)


# Android MediaRecorder (MPEG_4): major mp42, compatible isom and mp42.
_GENERIC_FTYP = _ftyp(b"mp42", b"isom", b"mp42")
# iPhone Voice Memos.
_VOICE_MEMO_FTYP = _ftyp(b"M4A ", b"M4A ", b"isom", b"mp42")
_MDAT = _box(b"mdat", b"\x21" * 32)
# moov after mdat, as a recorder that cannot seek back writes it.
_ANDROID_M4A = _GENERIC_FTYP + _MDAT + _moov(b"soun")
_ANDROID_M4A_MOOV_FIRST = _GENERIC_FTYP + _moov(b"soun") + _MDAT
_VOICE_MEMO_M4A = _VOICE_MEMO_FTYP + _MDAT + _moov(b"soun")
# iTunes and Apple's AAC encoder: the compatible brands end in an all-zero entry.
_ITUNES_FTYP = _ftyp(b"M4A ", b"M4A ", b"mp42", b"isom", bytes(4))
_ITUNES_M4A = _ITUNES_FTYP + _MDAT + _moov(b"soun")


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
        (_ogg_page(), "audio", "audio/ogg"),
        (b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A isom", "audio", "audio/mp4"),
        (_VOICE_MEMO_M4A, "audio", "audio/mp4"),
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
        b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ",
        b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomheic",
    ],
)
def test_classification_rejects_truncated_or_non_mp4_container_signatures(
    payload: bytes,
) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _ogg_page(capture=b"OggT"),
        _ogg_page(version=1),
        _ogg_page(header_type=0x00),  # not the start of a logical stream
        _ogg_page()[:26],
        _ogg_page(body=_opus_head()[:18]),  # identification one byte short
        _ogg_page(body=_opus_head(version=0x10)),  # unknown major version
        _ogg_page(body=_opus_head(channels=0)),
        _ogg_page(body=b"\x01vorbis\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"),
        _ogg_page(body=b"\x7fFLAC\x01\x00\x00\x00fLaC\x00\x00\x00\x22\x00\x00"),
        # declares a segment table longer than the bytes it actually carries
        _ogg_page(segment_count=255),
    ],
)
def test_classification_rejects_ogg_containers_without_an_opus_head(
    payload: bytes,
) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(payload)


@pytest.mark.parametrize("channels", [2, 3, 6, 8, 255])
@pytest.mark.parametrize("declared", ["audio/ogg", "audio/ogg; codecs=opus"])
def test_an_ogg_opus_that_is_not_mono_is_refused_with_its_own_code(
    channels: int, declared: str
) -> None:
    # The signature still says Opus: the refusal is about the channels, so the
    # screen can say "export it in mono" instead of "this format is not accepted".
    payload = _ogg_page(body=_opus_head(channels=channels))
    assert classify_media_payload(payload).mime_type == "audio/ogg"
    with pytest.raises(MediaAssetDomainError, match=r"^media_audio_not_mono$"):
        validate_media_classification(payload, declared)


def test_a_non_mono_ogg_over_the_audio_limit_is_told_to_be_mono_first() -> None:
    # Exporting to mono also shrinks the file, so the channels come first.
    head = _ogg_page(body=_opus_head(channels=2))
    payload = head + bytes(MEDIA_AUDIO_VIDEO_MAX_BYTES + 1 - len(head))
    with pytest.raises(MediaAssetDomainError, match=r"^media_audio_not_mono$"):
        validate_media_classification(payload, "audio/ogg")
    mono = _ogg_page() + bytes(len(payload) - len(head))
    with pytest.raises(MediaAssetDomainError, match=r"^media_size_exceeded$"):
        validate_media_classification(mono, "audio/ogg")


@pytest.mark.parametrize("channels", [1, 2, 8, 255])
def test_the_opus_locator_reads_the_identification_header_of_the_first_page(
    channels: int,
) -> None:
    head = _opus_head(channels=channels)
    assert ogg_opus_identification_header(_ogg_page(body=head)) == head
    assert ogg_opus_channel_count(_ogg_page(body=head)) == channels
    # A header packet past 254 bytes spans two lacing values (RFC 3533): the
    # packet starts after the whole segment table, not at a fixed offset.
    padded = head + bytes(300 - len(head))
    two_segments = _ogg_page(segments=bytes([255, 45]), body=padded)
    assert ogg_opus_identification_header(two_segments) == head
    assert ogg_opus_channel_count(two_segments) == channels


@pytest.mark.parametrize(
    "payload",
    [
        # OpusHead exactly where the first packet would start, but no capture
        # pattern: not an Ogg page, whatever follows.
        b"XXXX" + _ogg_page()[4:],
        b"oggs" + _ogg_page()[4:],
        _ogg_page(version=1),
        _ogg_page(header_type=0x00),
        _ogg_page(body=_opus_head(version=0x10)),
        _ogg_page(body=_opus_head()[:18]),
        _ogg_page()[:26],
        _ogg_page(segment_count=255),
        b"",
    ],
)
def test_the_opus_locator_finds_nothing_outside_a_first_ogg_opus_page(
    payload: bytes,
) -> None:
    assert ogg_opus_identification_header(payload) is None
    assert ogg_opus_channel_count(payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        # The lacing [10, 9] ends the first packet after 10 bytes: OpusHead is
        # split across two packets, so the first one is no ID header.
        pytest.param(_ogg_page(segments=bytes([10, 9])), id="first-packet-of-10-bytes"),
        pytest.param(_ogg_page(segments=bytes([18])), id="first-packet-of-18-bytes"),
        # A continued page carries the tail of an earlier packet.
        pytest.param(_ogg_page(header_type=0x03), id="continuation-flag"),
        pytest.param(_ogg_page(header_type=0x01), id="continuation-without-bos"),
        # No segments: the page holds no packet at all.
        pytest.param(_ogg_page(segments=b""), id="zero-segments"),
        # A lacing of 255 with no shorter value after it: the first packet
        # runs past the page, so the page does not hold the whole ID header.
        pytest.param(
            _ogg_page(segments=bytes([255]), body=_opus_head() + bytes(236)),
            id="first-packet-unterminated",
        ),
    ],
)
def test_a_first_page_that_does_not_hold_one_whole_id_header_is_not_opus(
    payload: bytes,
) -> None:
    assert ogg_opus_identification_header(payload) is None
    assert ogg_opus_channel_count(payload) is None
    with pytest.raises(MediaAssetDomainError, match=r"^media_signature_invalid$"):
        validate_media_classification(payload, "audio/ogg")


def test_an_id_header_ending_exactly_at_byte_301_is_read_from_a_301_byte_prefix() -> (
    None
):
    # 255 lacing values, the first of them the 19-byte ID header: OpusHead
    # sits at bytes 282 to 301, the farthest a valid first page can put it.
    head = _opus_head(channels=1)
    page = _ogg_page(segments=bytes([19]) + bytes(254), body=head)
    assert len(page) == 301
    assert ogg_opus_identification_header(page[:301]) == head
    assert ogg_opus_channel_count(page[:301]) == 1
    assert ogg_opus_channel_count(page[:300]) is None
    stereo = _ogg_page(segments=bytes([19]) + bytes(254), body=_opus_head(channels=2))
    assert ogg_opus_channel_count(stereo + bytes(1000)) == 2
    assert ogg_opus_channel_count((stereo + bytes(1000))[:301]) == 2


def _locator_cases(count: int) -> Iterator[bytes]:
    """Valid, mutated and random first pages, seeded so a failure replays."""

    rng = random.Random(7845)
    for _ in range(count):
        segments = bytes(
            rng.choice([0, 9, 10, 18, 19, 20, 254, 255, rng.randrange(256)])
            for _ in range(rng.choice([0, 1, 2, 3, rng.randrange(256), 255]))
        )
        page = bytearray(
            _ogg_page(
                header_type=rng.choice([0x00, 0x01, 0x02, 0x03, 0x06]),
                segments=segments,
                body=_opus_head(
                    version=rng.choice([0, 1, 15, 16]),
                    channels=rng.choice([0, 1, 2, 255]),
                )
                + rng.randbytes(rng.randrange(400)),
            )
        )
        for _ in range(rng.randrange(3)):
            page[rng.randrange(len(page))] = rng.randrange(256)
        yield bytes(page[: rng.choice([len(page), rng.randrange(len(page) + 1)])])
        yield rng.randbytes(rng.randrange(400))


def test_a_301_byte_prefix_always_gives_the_answer_of_the_whole_payload() -> None:
    # The sender's pre-claim check reads only the first 301 bytes; it must
    # never accept or refuse what the whole stored payload would not.
    for payload in _locator_cases(4000):
        whole = ogg_opus_identification_header(payload)
        assert ogg_opus_identification_header(payload[:301]) == whole, payload
        assert ogg_opus_channel_count(payload[:301]) == ogg_opus_channel_count(
            payload
        ), payload


# The ID header page plus the comment header page of ``ogg_opus_file``.
_HEADER_PAGES_SIZE = len(ogg_opus_file(0.0, audio_pages=0))


@pytest.mark.parametrize(
    ("seconds", "pre_skip"),
    [(0.5, 312), (61.0, 312), (121.0, 312), (299.98, 3840), (60.0, 0)],
)
def test_an_ogg_opus_duration_is_its_last_granule_minus_the_pre_skip(
    seconds: float, pre_skip: int
) -> None:
    payload = ogg_opus_file(seconds, pre_skip=pre_skip)
    assert ogg_opus_duration_seconds(payload) == pytest.approx(seconds, abs=1e-4)


def test_the_duration_does_not_depend_on_the_file_size() -> None:
    # The very thing a byte estimate gets wrong: the same 2 minutes in a tiny
    # file and in a padded one, split into many pages of large packets.
    small = ogg_opus_file(120.0, audio_pages=2, packet_size=10)
    large = ogg_opus_file(120.0, audio_pages=40, packet_size=1000)
    assert len(large) > 100 * len(small)
    assert ogg_opus_duration_seconds(small) == pytest.approx(120.0)
    assert ogg_opus_duration_seconds(large) == pytest.approx(120.0)


def test_a_header_only_ogg_opus_lasts_zero_seconds() -> None:
    head = ogg_page(opus_identification_header(), granule=0, header_type=0x02)
    tags = ogg_page(b"OpusTags" + bytes(8), granule=0)
    assert ogg_opus_duration_seconds(head + tags) == 0.0


def test_a_page_where_no_packet_ends_keeps_the_previous_granule() -> None:
    # Granule -1: the page only continues a packet (RFC 3533), it says nothing
    # about the sample count, and must not be read as a huge unsigned number.
    payload = ogg_opus_file(30.0) + ogg_page(bytes(40), granule=-1)
    assert ogg_opus_duration_seconds(payload) == pytest.approx(30.0)


def test_only_the_opus_stream_counts_in_a_multiplexed_file() -> None:
    other = ogg_page(bytes(40), granule=48_000 * 3600, serial=b"\x01\x02\x03\x04")
    assert ogg_opus_duration_seconds(ogg_opus_file(30.0) + other) == pytest.approx(30.0)


def test_a_negative_duration_is_clamped_to_zero() -> None:
    # A last granule below the pre-skip is a broken stream, never a duration
    # below zero that would pass any limit check backwards.
    head = ogg_page(
        opus_identification_header(pre_skip=3840), granule=0, header_type=0x02
    )
    assert ogg_opus_duration_seconds(head + ogg_page(bytes(40), granule=100)) == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"ID3\x04" + bytes(400), id="mp3"),
        pytest.param(b"#!AMR\n" + bytes(400), id="amr"),
        pytest.param(b"", id="empty"),
        pytest.param(
            ogg_page(b"\x01vorbis" + bytes(23), granule=0, header_type=0x02)
            + ogg_page(bytes(40), granule=48_000 * 30),
            id="ogg_vorbis",
        ),
        pytest.param(ogg_opus_file(30.0)[:-1], id="truncated_last_page"),
        pytest.param(ogg_opus_file(30.0) + b"\x00", id="trailing_byte"),
        pytest.param(ogg_opus_file(30.0) + b"OggS" + bytes(10), id="truncated_header"),
        pytest.param(
            ogg_opus_file(30.0)[:_HEADER_PAGES_SIZE]
            + b"junk"
            + ogg_opus_file(30.0)[_HEADER_PAGES_SIZE:],
            id="bytes_between_pages",
        ),
        pytest.param(
            ogg_opus_file(30.0) + b"OggS\x01" + ogg_page(bytes(40), granule=1)[5:],
            id="unknown_page_version",
        ),
        pytest.param(
            ogg_opus_file(30.0)
            + ogg_page(bytes(40), granule=1, segments=bytes([255, 255, 255])),
            id="packet_past_the_end",
        ),
        pytest.param(
            ogg_opus_file(30.0) + ogg_page(b"", granule=1, segments=b"")[:26] + b"\xc8",
            id="segment_table_past_the_end",
        ),
    ],
)
def test_a_payload_without_a_whole_ogg_opus_stream_has_no_duration(
    payload: bytes,
) -> None:
    assert ogg_opus_duration_seconds(payload) is None


def test_a_packet_spread_over_several_lacing_values_is_walked_whole() -> None:
    # 600 bytes = lacing 255, 255, 90: the page ends after their sum, not
    # after the first value.
    payload = ogg_opus_file(45.0, packet_size=600)
    assert ogg_opus_duration_seconds(payload) == pytest.approx(45.0)


def _mutated_ogg_opus_files(count: int) -> Iterator[bytes]:
    """Whole multi-page Ogg Opus files with a few bytes changed, some cut."""

    rng = random.Random(7845)
    for _ in range(count):
        payload = bytearray(
            ogg_opus_file(
                rng.uniform(0, 300),
                pre_skip=rng.randrange(65536),
                audio_pages=rng.randrange(1, 8),
                packet_size=rng.randrange(1, 700),
            )
        )
        for _ in range(rng.randrange(4)):
            payload[rng.randrange(len(payload))] = rng.randrange(256)
        cut = rng.choice([len(payload), rng.randrange(len(payload) + 1)])
        yield bytes(payload[:cut])


def test_the_duration_never_raises_on_arbitrary_bytes() -> None:
    walked = 0
    for payload in [*_locator_cases(2000), *_mutated_ogg_opus_files(3000)]:
        result = ogg_opus_duration_seconds(payload)
        assert result is None or result >= 0.0, payload
        walked += ogg_opus_identification_header(payload) is not None
    # The page walk itself is exercised, not only the OpusHead locator.
    assert walked > 1500


def test_every_stored_format_has_one_server_made_file_extension() -> None:
    # One table names the download fallback and the Meta upload; a MIME added
    # to the allowlist without an extension would be a KeyError on both.
    assert set(MEDIA_FILE_EXTENSIONS) == MEDIA_CANONICAL_MIME_TYPES
    # Meta's supported-media table spells JPEG as .jpeg (error 131053).
    assert MEDIA_FILE_EXTENSIONS == {
        "image/jpeg": ".jpeg",
        "image/png": ".png",
        "application/pdf": ".pdf",
        "audio/mpeg": ".mp3",
        "audio/ogg": ".ogg",
        "audio/mp4": ".m4a",
        "video/mp4": ".mp4",
    }


@pytest.mark.parametrize(
    "declared",
    [
        "audio/ogg",
        "audio/ogg; codecs=opus",
        "AUDIO/OGG",
        "  audio/ogg ;codecs=opus  ",
    ],
)
def test_ogg_is_accepted_however_the_sender_spells_its_content_type(
    declared: str,
) -> None:
    classification = validate_media_classification(_ogg_page(), declared)
    assert (classification.category, classification.mime_type) == (
        "audio",
        "audio/ogg",
    )
    assert canonical_declared_mime(declared) == "audio/ogg"


@pytest.mark.parametrize(
    "payload",
    [
        _ftyp(b"M4B ", b"M4B ", b"isom", b"mp42"),  # audiobook
        _ftyp(b"M4P ", b"M4P ", b"isom", b"mp42"),  # FairPlay DRM
        _ftyp(b"M4V ", b"M4V ", b"isom", b"mp42"),  # video
        # As the major brand alone, with nothing but MP4 brands after it.
        _ftyp(b"M4B ", b"isom", b"mp42"),
        _ftyp(b"M4P ", b"isom", b"mp42"),
        _ftyp(b"M4V ", b"isom", b"mp42"),
        _ftyp(b"M4A ", b"M4A ", b"M4B "),
        _ftyp(b"M4A ", b"M4A ", b"M4P "),
        _ftyp(b"M4A ", b"M4A ", b"M4V "),
        _ftyp(b"M4A ", b"M4A ", b"qt  "),
        _ftyp(b"isom", b"isom", b"M4A "),  # M4A only counts as the major brand
        _ftyp(b"M4V ", b"M4A ") + _MDAT + _moov(b"soun"),
        _ftyp(b"M4A ")[:15],  # an ftyp box too short to carry a minor version
    ],
)
def test_m4b_m4p_and_m4v_are_not_recognized(payload: bytes) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(payload)
    for declared in ("audio/mp4", "audio/x-m4a", "video/mp4"):
        with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
            validate_media_classification(payload, declared)


@pytest.mark.parametrize(
    ("declared", "canonical"),
    [
        ("audio/mp4", "audio/mp4"),
        ("audio/x-m4a", "audio/mp4"),
        ("audio/m4a", "audio/mp4"),
        ("Audio/X-M4A", "audio/mp4"),
        ("  audio/x-m4a ; codecs=mp4a.40.2 ", "audio/mp4"),
        ("audio/mp4; codecs=mp4a.40.2", "audio/mp4"),
        # Only the .m4a spellings are aliases.
        ("audio/x-m4b", "audio/x-m4b"),
        ("audio/x-m4p", "audio/x-m4p"),
        ("video/x-m4v", "video/x-m4v"),
        ("audio/aac", "audio/aac"),
    ],
)
def test_the_m4a_labels_are_spelled_audio_mp4(declared: str, canonical: str) -> None:
    assert canonical_declared_mime(declared) == canonical


@pytest.mark.parametrize("declared", ["audio/x-m4b", "video/x-m4v", "audio/aac"])
def test_labels_that_are_not_m4a_stay_unsupported(declared: str) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_type_unsupported"):
        validate_media_classification(_VOICE_MEMO_M4A, declared)


@pytest.mark.parametrize("declared", ["audio/mp4", "audio/x-m4a", "audio/m4a"])
@pytest.mark.parametrize(
    "payload",
    [
        _VOICE_MEMO_FTYP,  # the brand alone decides: no box walk
        _VOICE_MEMO_M4A,
        _ftyp(b"M4A ", b"M4A ", b"mp42", b"isom"),
        _ftyp(b"M4A ", b"M4A ", b"isom", b"iso2"),  # Voice Memos once copied
        _ftyp(b"M4A ") + _MDAT + _moov(b"soun"),  # no compatible brand at all
        _ITUNES_FTYP,
        _ITUNES_M4A,
        _ftyp(b"M4A ", bytes(4), b"M4A ", bytes(4)),  # padding anywhere, repeated
    ],
    ids=[
        "brand-only",
        "voice-memo",
        "mp42isom",
        "isomiso2",
        "major-only",
        "itunes-brand-only",
        "itunes",
        "padding-anywhere",
    ],
)
def test_rule_a_the_m4a_major_brand_is_audio_mp4(payload: bytes, declared: str) -> None:
    classification = validate_media_classification(payload, declared)
    assert (classification.category, classification.mime_type) == (
        "audio",
        "audio/mp4",
    )
    assert classify_media_payload(payload) == classification


def test_the_itunes_ftyp_is_the_one_apple_writes() -> None:
    # 0x20 bytes: M4A, minor 0, then M4A mp42 isom and the zero padding.
    assert _ITUNES_FTYP == (
        b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00M4A mp42isom\x00\x00\x00\x00"
    )


@pytest.mark.parametrize(
    "payload",
    [
        # All zero, the major brand included.
        _ftyp(bytes(4), bytes(4)),
        _ftyp(bytes(4)),
        b"\x00\x00\x00\x20ftyp" + bytes(24),
        # The padding never stands in for the major brand.
        _ftyp(bytes(4), b"M4A ", b"mp42", b"isom") + _MDAT + _moov(b"soun"),
        # ...and never widens video/mp4.
        _ftyp(b"mp42", b"isom", b"mp42", bytes(4)) + _MDAT + _moov(b"soun"),
        _ftyp(b"isom", b"isom", bytes(4)) + _MDAT + _moov(b"vide", b"soun"),
    ],
    ids=[
        "all-zero",
        "zero-major-only",
        "zero-ftyp",
        "zero-major-m4a-compatible",
        "generic-padded-audio",
        "generic-padded-video",
    ],
)
def test_the_zero_padding_brand_is_accepted_for_m4a_only(payload: bytes) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(payload)
    for declared in ("audio/mp4", "audio/x-m4a", "video/mp4"):
        with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
            validate_media_classification(payload, declared)


@pytest.mark.parametrize(
    "payload",
    [
        # Android's MPEG4Writer for 3GPP output, and for MPEG_4 on older releases.
        _ftyp(b"3gp4", b"isom", b"3gp4") + _MDAT + _moov(b"soun"),
        _ftyp(b"isom", b"isom", b"3gp4") + _MDAT + _moov(b"soun"),
        _ftyp(b"3gp5", b"3gp5", b"isom") + _MDAT + _moov(b"soun"),
        _ftyp(b"mp42", b"isom", b"3gp5") + _MDAT + _moov(b"soun"),
        _ftyp(b"M4A ", b"M4A ", b"3gp4") + _MDAT + _moov(b"soun"),
    ],
    ids=["3gp4-major", "3gp4-compatible", "3gp5-major", "3gp5-compatible", "m4a-3gp4"],
)
def test_3gpp_brands_stay_refused_even_audio_only(payload: bytes) -> None:
    """Known limitation, deliberately: those recorders write AMR, and rule (b)
    checks the handler type, not the codec, so accepting them would store AMR
    as audio/mp4."""
    for declared in ("audio/mp4", "audio/x-m4a", "audio/m4a", "video/mp4"):
        with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
            validate_media_classification(payload, declared)


def test_rule_a_an_m4a_brand_declared_video_is_a_mismatch() -> None:
    with pytest.raises(MediaAssetDomainError, match="media_type_mismatch"):
        validate_media_classification(_VOICE_MEMO_M4A, "video/mp4")


@pytest.mark.parametrize("declared", ["audio/mp4", "audio/x-m4a", "audio/m4a"])
@pytest.mark.parametrize(
    "payload",
    [
        _ANDROID_M4A,
        _ANDROID_M4A_MOOV_FIRST,
        # ffmpeg's mp4 muxer.
        _ftyp(b"isom", b"isom", b"iso2", b"mp41") + _MDAT + _moov(b"soun"),
        # Other non-video handlers ride along.
        _GENERIC_FTYP + _MDAT + _moov(b"soun", b"meta", b"text"),
        _GENERIC_FTYP + _MDAT + _moov(b"soun", b"soun"),
        _GENERIC_FTYP + _box(b"free", bytes(16)) + _MDAT + _moov(b"soun"),
    ],
    ids=["moov-last", "moov-first", "ffmpeg", "meta-text", "two-soun", "free"],
)
def test_rule_b_a_generic_brand_declared_audio_is_audio_mp4_when_its_tracks_prove_it(
    payload: bytes, declared: str
) -> None:
    # The signature alone still says video: the declared MIME decides the walk.
    assert classify_media_payload(payload).mime_type == "video/mp4"
    classification = validate_media_classification(payload, declared)
    assert (classification.category, classification.mime_type) == (
        "audio",
        "audio/mp4",
    )


@pytest.mark.parametrize(
    "payload",
    [
        _GENERIC_FTYP + _MDAT + _moov(b"vide", b"soun"),
        _GENERIC_FTYP + _MDAT + _moov(b"soun", b"vide"),
        _GENERIC_FTYP + _moov(b"vide") + _MDAT,
        _GENERIC_FTYP + _MDAT + _moov(b"soun") + _moov(b"vide"),
        # No soun track at all.
        _GENERIC_FTYP + _MDAT,
        _GENERIC_FTYP + _MDAT + _moov(),
        _GENERIC_FTYP + _MDAT + _moov(b"meta"),
        _GENERIC_FTYP + _MDAT + _box(b"moov", _box(b"trak", _box(b"tkhd"))),
        # A handler outside moov -> trak -> mdia is not a track's.
        _GENERIC_FTYP + _MDAT + _box(b"moov", _box(b"udta", _hdlr(b"soun"))),
        _GENERIC_FTYP + _MDAT + _box(b"moov", _box(b"trak", _hdlr(b"soun"))),
        _GENERIC_FTYP + _MDAT + _box(b"free", _moov(b"soun")),
        # Well formed to the end of the file, and still no moov.
        _GENERIC_FTYP + _to_end_box(b"mdat", _moov(b"soun")),
    ],
    ids=[
        "vide-first",
        "vide-last",
        "vide-only",
        "second-moov-vide",
        "no-moov",
        "no-trak",
        "meta-only",
        "no-mdia",
        "udta-hdlr",
        "trak-hdlr",
        "moov-inside-free",
        "to-end-mdat-swallows-moov",
    ],
)
def test_rule_b_a_generic_brand_without_audio_only_tracks_is_a_mismatch(
    payload: bytes,
) -> None:
    for declared in ("audio/mp4", "audio/x-m4a"):
        with pytest.raises(MediaAssetDomainError, match="media_type_mismatch"):
            validate_media_classification(payload, declared)


@pytest.mark.parametrize(
    "payload",
    [
        _ANDROID_M4A,
        _GENERIC_FTYP + _MDAT + _moov(b"vide", b"soun"),
        # Structure the walk would refuse: video never walks, so it never sees it.
        _GENERIC_FTYP + b"\x00\x00\x00",
        _GENERIC_FTYP + _box(b"mdat", bytes(8))[:-1],
        b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42",
    ],
    ids=["audio-only", "audio-video", "trailing-bytes", "truncated-mdat", "bare"],
)
def test_rule_c_a_generic_brand_declared_video_is_video_exactly_as_before(
    payload: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_walk(_payload: bytes) -> frozenset[bytes]:
        raise AssertionError("a video/mp4 declaration walked the boxes")

    monkeypatch.setattr(media_assets, "_mp4_track_handlers", no_walk)
    classification = validate_media_classification(payload, "video/mp4")
    assert (classification.category, classification.mime_type) == (
        "video",
        "video/mp4",
    )


@pytest.mark.parametrize(
    ("payload", "declared"),
    [
        (_VOICE_MEMO_M4A, "audio/mp4"),
        (_ogg_page(), "audio/ogg"),
        (b"\xff\xfb\x90\x64mp3", "audio/mpeg"),
        (b"%PDF-1.7\nbody", "application/pdf"),
    ],
)
def test_only_a_generic_brand_declared_audio_mp4_walks_the_boxes(
    payload: bytes, declared: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_walk(_payload: bytes) -> frozenset[bytes]:
        raise AssertionError("walked the boxes of a payload its signature decides")

    monkeypatch.setattr(media_assets, "_mp4_track_handlers", no_walk)
    assert validate_media_classification(payload, declared).mime_type == declared


@pytest.mark.parametrize(
    "payload",
    [
        # 64-bit largesize (size 1) on the mdat, on the moov and on a nested box.
        _GENERIC_FTYP + _large_box(b"mdat", b"\x21" * 32) + _moov(b"soun"),
        _GENERIC_FTYP + _MDAT + _large_box(b"moov", _trak(b"soun")),
        _GENERIC_FTYP
        + _MDAT
        + _box(b"moov", _large_box(b"trak", _box(b"mdia", _hdlr(b"soun")))),
        # Size 0: the last top-level box runs to the end of the file.
        _GENERIC_FTYP + _moov(b"soun") + _to_end_box(b"mdat", b"\x21" * 32),
        _GENERIC_FTYP + _MDAT + _to_end_box(b"moov", _trak(b"soun")),
        # ...and a nested one to the end of its parent, which here is not the
        # end of the file: a free box follows the moov.
        _GENERIC_FTYP
        + _MDAT
        + _box(b"moov", _box(b"trak", _to_end_box(b"mdia", _hdlr(b"soun"))))
        + _box(b"free", bytes(8)),
    ],
    ids=[
        "largesize-mdat",
        "largesize-moov",
        "largesize-trak",
        "to-end-mdat",
        "to-end-moov",
        "to-end-nested",
    ],
)
def test_the_box_walk_reads_largesize_and_size_zero(payload: bytes) -> None:
    assert validate_media_classification(payload, "audio/mp4").mime_type == (
        "audio/mp4"
    )


@pytest.mark.parametrize(
    "payload",
    [
        _ANDROID_M4A + b"\x00",  # a trailing byte too short for a box header
        _ANDROID_M4A + bytes(7),
        _ANDROID_M4A[:-1],  # moov one byte short
        _GENERIC_FTYP + _box(b"mdat", bytes(4))[:-1] + _moov(b"soun"),
        # A size below its own header.
        _GENERIC_FTYP + (4).to_bytes(4, "big") + b"free" + _moov(b"soun"),
        _GENERIC_FTYP + (7).to_bytes(4, "big") + b"free" + _moov(b"soun"),
        # largesize below 16, past the end, absurd, or cut short.
        _GENERIC_FTYP
        + (1).to_bytes(4, "big")
        + b"mdat"
        + (15).to_bytes(8, "big")
        + _moov(b"soun"),
        # largesize 12: were the 16-byte header not enforced, the walk would go
        # on cleanly from offset 12 (the largesize's low word reads as a 12-byte
        # free box) to a moov with a soun track, and accept the file.
        _GENERIC_FTYP
        + (1).to_bytes(4, "big")
        + b"mdat"
        + (12).to_bytes(8, "big")
        + b"free"
        + bytes(4)
        + _moov(b"soun"),
        _GENERIC_FTYP
        + (1).to_bytes(4, "big")
        + b"mdat"
        + (10**6).to_bytes(8, "big")
        + _moov(b"soun"),
        _GENERIC_FTYP + (1).to_bytes(4, "big") + b"mdat" + b"\xff" * 8,
        _GENERIC_FTYP + _moov(b"soun") + (1).to_bytes(4, "big") + b"mdat\x00\x00",
        # A box larger than its parent.
        _GENERIC_FTYP
        + _MDAT
        + _box(b"moov", (10**4).to_bytes(4, "big") + b"trak" + _hdlr(b"soun")),
        # An hdlr too short to carry a handler_type.
        _GENERIC_FTYP
        + _MDAT
        + _box(b"moov", _box(b"trak", _box(b"mdia", _box(b"hdlr", bytes(11))))),
    ],
    ids=[
        "trailing-byte",
        "trailing-seven",
        "moov-short",
        "mdat-short",
        "size-4",
        "size-7",
        "largesize-15",
        "largesize-12-realigns",
        "largesize-past-end",
        "largesize-max",
        "largesize-cut",
        "child-past-parent",
        "hdlr-short",
    ],
)
def test_a_malformed_box_structure_is_an_invalid_signature(payload: bytes) -> None:
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        validate_media_classification(payload, "audio/mp4")


def test_the_box_walk_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # ftyp, mdat, moov; mvhd, trak; tkhd, mdia; mdhd, hdlr, minf.
    boxes = 10
    monkeypatch.setattr(media_assets, "_MP4_BOX_WALK_LIMIT", boxes)
    assert validate_media_classification(_ANDROID_M4A, "audio/mp4").mime_type == (
        "audio/mp4"
    )
    monkeypatch.setattr(media_assets, "_MP4_BOX_WALK_LIMIT", boxes - 1)
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        validate_media_classification(_ANDROID_M4A, "audio/mp4")


def test_a_payload_of_tiny_boxes_stops_at_the_walk_limit() -> None:
    limit = media_assets._MP4_BOX_WALK_LIMIT
    flood = _GENERIC_FTYP + _box(b"free") * limit + _moov(b"soun")
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        validate_media_classification(flood, "audio/mp4")
    # One box fewer than the limit, everything counted, still walks to the end.
    within = _GENERIC_FTYP + _box(b"free") * (limit - 10) + _moov(b"soun")
    assert validate_media_classification(within, "audio/mp4").mime_type == ("audio/mp4")


@pytest.mark.parametrize(
    ("handlers", "outcome"),
    [((b"soun",), "audio/mp4"), ((b"vide", b"soun"), "media_type_mismatch")],
    ids=["audio", "audio-video"],
)
def test_a_fragmented_recording_with_thousands_of_fragments_is_walked(
    handlers: tuple[bytes, ...], outcome: str
) -> None:
    # MediaRecorder writes one moof and one mdat per timeslice.
    fragment = _box(b"moof", _box(b"mfhd", bytes(8))) + _box(b"mdat", b"\x21" * 16)
    fragments = 6_000
    payload = (
        _ftyp(b"iso5", b"iso5", b"iso6", b"mp41")
        + _moov(*handlers)
        + fragment * fragments
    )
    # More top-level boxes than the 10,000 the walk first allowed in total.
    assert 2 + 2 * fragments > 10_000
    assert _outcome(payload, "audio/mp4") == outcome


def test_the_ftyp_brands_are_read_once_and_never_materialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passes: list[int] = []
    original = media_assets._ftyp_brands

    def counted(payload: bytes, box_size: int) -> Iterator[bytes]:
        passes.append(0)
        for brand in original(payload, box_size):
            passes[-1] += 1
            yield brand

    monkeypatch.setattr(media_assets, "_ftyp_brands", counted)
    compatible = 250_000
    for major, brand, mime_type in (
        (b"isom", b"isom", "video/mp4"),
        (b"M4A ", b"mp42", "audio/mp4"),
    ):
        payload = _ftyp(major, *([brand] * compatible))
        passes.clear()
        tracemalloc.start()
        try:
            classification = classify_media_payload(payload)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert classification.mime_type == mime_type
        # One pass for both brand rules, each brand read once...
        assert passes == [compatible + 1]
        # ...and never held together: the brands alone would be megabytes.
        assert peak < 256 * 1024
    # The first brand outside the rule ends the pass.
    passes.clear()
    flooded = _ftyp(b"isom", b"isom", b"qt  ", *([b"isom"] * compatible))
    with pytest.raises(MediaAssetDomainError, match="media_signature_invalid"):
        classify_media_payload(flooded)
    assert passes == [3]


_WALK_FIXTURES = {
    "moov-last": _ANDROID_M4A,
    "moov-first": _ANDROID_M4A_MOOV_FIRST,
    "largesize": _GENERIC_FTYP + _large_box(b"mdat", b"\x21" * 8) + _moov(b"soun"),
    "to-end": _GENERIC_FTYP + _moov(b"soun") + _to_end_box(b"mdat", b"\x21" * 8),
    "voice-memo": _VOICE_MEMO_M4A,
    "itunes": _ITUNES_M4A,
    "audio-video": _GENERIC_FTYP + _MDAT + _moov(b"vide", b"soun"),
}


def _outcome(payload: bytes, declared: str) -> str:
    try:
        return validate_media_classification(payload, declared).mime_type
    except MediaAssetDomainError as error:
        return error.code


@pytest.mark.parametrize("name", sorted(_WALK_FIXTURES))
def test_truncation_at_every_byte_offset_fails_only_with_the_domain_error(
    name: str,
) -> None:
    fixture = _WALK_FIXTURES[name]
    accepted_prefixes = []
    for cut in range(len(fixture) + 1):
        prefix = fixture[:cut]
        for declared in ("audio/mp4", "audio/x-m4a", "video/mp4"):
            outcome = _outcome(prefix, declared)  # anything else propagates
            assert outcome in {
                "audio/mp4",
                "video/mp4",
                "media_signature_invalid",
                "media_type_mismatch",
            }, (cut, declared, outcome)
            if declared == "audio/mp4" and outcome == "audio/mp4":
                accepted_prefixes.append(cut)
        try:
            classify_media_payload(prefix)
        except MediaAssetDomainError:
            pass
        try:
            media_assets._mp4_track_handlers(prefix)
        except MediaAssetDomainError:
            pass
    brand_decided = {"voice-memo": _VOICE_MEMO_FTYP, "itunes": _ITUNES_FTYP}
    if name in brand_decided:
        # The brand decides: every prefix holding the whole ftyp is accepted.
        ftyp_end = len(brand_decided[name])
        assert accepted_prefixes == list(range(ftyp_end, len(fixture) + 1))
    elif name == "audio-video":
        assert accepted_prefixes == []
    elif name == "moov-first":
        # Cut exactly after moov it is a well-formed file without its mdat.
        moov_end = len(_GENERIC_FTYP) + len(_moov(b"soun"))
        assert accepted_prefixes == [moov_end, len(fixture)]
    elif name == "to-end":
        # A size-0 box ends wherever the file does, so no cut after its header
        # is detectable; the cut exactly after moov is a file without mdat.
        moov_end = len(_GENERIC_FTYP) + len(_moov(b"soun"))
        assert accepted_prefixes == [moov_end, *range(moov_end + 8, len(fixture) + 1)]
    else:
        # A generic brand is audio only when the whole file tiles into boxes.
        assert accepted_prefixes == [len(fixture)]


@pytest.mark.parametrize("name", sorted(_WALK_FIXTURES))
@pytest.mark.parametrize("value", [0x00, 0x01, 0x7F, 0xFF])
def test_overwriting_any_byte_fails_only_with_the_domain_error(
    name: str, value: int
) -> None:
    fixture = _WALK_FIXTURES[name]
    for offset in range(len(fixture)):
        corrupted = fixture[:offset] + bytes([value]) + fixture[offset + 1 :]
        for declared in ("audio/mp4", "video/mp4"):
            assert _outcome(corrupted, declared) in {
                "audio/mp4",
                "video/mp4",
                "media_signature_invalid",
                "media_type_mismatch",
            }


def test_each_category_is_capped_at_the_published_limit() -> None:
    assert MEDIA_CATEGORY_MAX_BYTES == {
        "image": 5_000_000,
        "pdf": 20_000_000,
        "audio": 16_000_000,
        "video": 16_000_000,
    }
    # The PDF's 20 MB is the widest, so it is what the pre-parser cap derives from.
    assert media_assets.MEDIA_FILE_MAX_BYTES == MEDIA_PDF_MAX_BYTES


@pytest.mark.parametrize(
    ("prefix", "mime_type", "limit"),
    [
        (b"\x89PNG\r\n\x1a\n", "image/png", MEDIA_IMAGE_MAX_BYTES),
        (b"\xff\xd8\xff\xe0", "image/jpeg", MEDIA_IMAGE_MAX_BYTES),
        (b"%PDF-1.7\n", "application/pdf", MEDIA_PDF_MAX_BYTES),
        (b"\xff\xfb\x90\x64", "audio/mpeg", MEDIA_AUDIO_VIDEO_MAX_BYTES),
        (_VOICE_MEMO_FTYP, "audio/mp4", MEDIA_AUDIO_VIDEO_MAX_BYTES),
    ],
)
def test_a_payload_one_byte_over_its_category_limit_is_rejected(
    prefix: bytes,
    mime_type: str,
    limit: int,
) -> None:
    at_limit = prefix + b"\x00" * (limit - len(prefix))
    assert validate_media_classification(at_limit, mime_type).mime_type == mime_type
    with pytest.raises(MediaAssetDomainError, match="media_size_exceeded"):
        validate_media_classification(at_limit + b"\x00", mime_type)


def test_a_generic_brand_audio_mp4_is_capped_at_the_audio_limit() -> None:
    def android(size: int) -> bytes:
        moov = _moov(b"soun")
        mdat_body = size - len(_GENERIC_FTYP) - len(moov) - 8
        return _GENERIC_FTYP + _box(b"mdat", bytes(mdat_body)) + moov

    at_limit = android(MEDIA_AUDIO_VIDEO_MAX_BYTES)
    assert len(at_limit) == MEDIA_AUDIO_VIDEO_MAX_BYTES
    assert validate_media_classification(at_limit, "audio/x-m4a").category == "audio"
    with pytest.raises(MediaAssetDomainError, match="media_size_exceeded"):
        validate_media_classification(
            android(MEDIA_AUDIO_VIDEO_MAX_BYTES + 1), "audio/x-m4a"
        )


@pytest.mark.asyncio
async def test_bounded_reader_stops_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media_assets, "MEDIA_FILE_MAX_BYTES", 4)
    monkeypatch.setattr(media_assets, "MEDIA_READ_CHUNK_BYTES", 2)
    assert await read_media_payload_limited(_ByteStream(b"1234")) == b"1234"
    with pytest.raises(MediaAssetDomainError, match="media_size_exceeded"):
        await read_media_payload_limited(_ByteStream(b"12345"))


_GUARD_BUILDER_ID = UUID("00000000-0000-4000-8000-0000000000b1")
_GUARD_TENANT_ID = UUID("00000000-0000-4000-8000-0000000000c1")
_GUARD_ACTOR_ID = UUID("00000000-0000-4000-8000-0000000000a1")


class _GuardResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value

    def one_or_none(self) -> object:
        return self.value


class _GuardSession:
    """Statement-recording session that answers the guard's four lock queries."""

    def __init__(
        self,
        *,
        actor_exists: bool = True,
        builder_exists: bool = True,
        tenant_exists: bool = True,
        current_builder_id: UUID | None = _GUARD_BUILDER_ID,
        used_bytes: int = 0,
    ) -> None:
        self.actor_exists = actor_exists
        self.builder_exists = builder_exists
        self.tenant_exists = tenant_exists
        self.current_builder_id = current_builder_id
        self.used_bytes = used_bytes
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any]] = []
        self.rollbacks = 0
        self.new: list[object] = []
        self.no_autoflush = nullcontext()

    async def execute(self, statement: Any) -> _GuardResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = " ".join(str(compiled).lower().split())
        self.statements.append(sql)
        self.parameters.append(dict(compiled.params))
        if "pg_advisory_xact_lock" in sql:
            return _GuardResult(None)
        if "sum(media_assets.size_bytes)" in sql:
            return _GuardResult(self.used_bytes)
        if "from users" in sql:
            return _GuardResult(_GUARD_ACTOR_ID if self.actor_exists else None)
        if "from builders" in sql:
            return _GuardResult(_GUARD_BUILDER_ID if self.builder_exists else None)
        if "from tenants" in sql:
            if not self.tenant_exists:
                return _GuardResult(None)
            return _GuardResult(
                SimpleNamespace(
                    id=_GUARD_TENANT_ID,
                    builder_id=self.current_builder_id,
                )
            )
        raise AssertionError(f"unexpected statement: {sql}")

    async def rollback(self) -> None:
        self.rollbacks += 1

    def index(self, fragment: str) -> int:
        return next(
            position for position, sql in enumerate(self.statements) if fragment in sql
        )


def _guard_tenant(builder_id: UUID | None = _GUARD_BUILDER_ID) -> Tenant:
    return Tenant(id=_GUARD_TENANT_ID, name="Guard Clinic", builder_id=builder_id)


def _guard(
    session: _GuardSession,
    *,
    actor: UUID | None = _GUARD_ACTOR_ID,
    tenant: Tenant | None = None,
) -> MediaStorageCapacityGuard:
    return MediaStorageCapacityGuard(
        session,  # type: ignore[arg-type]
        actor,
        tenant if tenant is not None else _guard_tenant(),
    )


@pytest.mark.asyncio
async def test_guard_locks_user_builder_advisory_tenant_before_any_media_query() -> (
    None
):
    session = _GuardSession(used_bytes=40)
    guard = _guard(session)

    async with guard:
        entered = list(session.statements)
        projection = await guard.validate_projection(2)

    assert len(entered) == 4
    assert "from users" in entered[0] and "for key share" in entered[0]
    assert "from builders" in entered[1] and "for update" in entered[1]
    assert "pg_advisory_xact_lock" in entered[2]
    assert "from tenants" in entered[3] and "for update" in entered[3]
    assert not any("media_assets" in sql for sql in entered)
    expected_key = media_storage_advisory_key(_GUARD_BUILDER_ID)
    assert guard.owner_id == _GUARD_BUILDER_ID
    assert guard.advisory_key == expected_key
    assert expected_key in session.parameters[2].values()
    assert session.statements[4:] == [
        sql for sql in session.statements if "sum(media_assets.size_bytes)" in sql
    ]
    assert projection == MediaStorageProjection(
        owner_id=_GUARD_BUILDER_ID,
        used_bytes=40,
        additional_bytes=2,
        projected_bytes=42,
        cap_bytes=500_000_000,
    )
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_userless_standalone_guard_skips_user_and_builder_locks() -> None:
    session = _GuardSession(current_builder_id=None)
    guard = _guard(session, actor=None, tenant=_guard_tenant(builder_id=None))

    async with guard:
        await guard.validate_projection(0)

    assert guard.owner_id == _GUARD_TENANT_ID
    assert "pg_advisory_xact_lock" in session.statements[0]
    assert "from tenants" in session.statements[1]
    assert media_storage_advisory_key(_GUARD_TENANT_ID) in (
        session.parameters[0].values()
    )
    assert not any(
        "from users" in sql or "from builders" in sql for sql in session.statements
    )


@pytest.mark.asyncio
async def test_guard_enforces_exact_500mb_cap_boundary() -> None:
    assert MEDIA_STORAGE_CAP_BYTES == 500_000_000
    at_cap = _GuardSession(used_bytes=499_999_990)
    guard = _guard(at_cap)
    async with guard:
        projection = await guard.validate_projection(10)
    assert projection.projected_bytes == MEDIA_STORAGE_CAP_BYTES

    above_cap = _GuardSession(used_bytes=499_999_990)
    with pytest.raises(MediaStorageCapacityExceeded) as exceeded:
        async with _guard(above_cap) as guard:
            await guard.validate_projection(11)
    assert exceeded.value.code == "media_storage_cap_exceeded"
    assert above_cap.rollbacks == 1


@pytest.mark.asyncio
async def test_validation_before_or_after_the_context_is_rejected() -> None:
    session = _GuardSession()
    guard = _guard(session)

    with pytest.raises(MediaStorageGuardError, match="not active"):
        await guard.validate_projection(1)
    assert session.statements == []

    async with guard:
        await guard.validate_projection(1)
    measured = len(session.statements)
    with pytest.raises(MediaStorageGuardError, match="not active"):
        await guard.validate_projection(1)
    assert len(session.statements) == measured


@pytest.mark.asyncio
@pytest.mark.parametrize("second_size", [1, 2])
async def test_second_validation_is_rejected_under_the_same_lock(
    second_size: int,
) -> None:
    session = _GuardSession()
    async with _guard(session) as guard:
        await guard.validate_projection(1)
        with pytest.raises(MediaStorageGuardError, match="already validated"):
            await guard.validate_projection(second_size)
    assert sum("sum(media_assets.size_bytes)" in sql for sql in session.statements) == 1


@pytest.mark.asyncio
async def test_validation_after_cap_failure_cannot_be_retried() -> None:
    session = _GuardSession(used_bytes=MEDIA_STORAGE_CAP_BYTES)
    with pytest.raises(MediaStorageGuardError, match="not approved"):
        async with _guard(session) as guard:
            with pytest.raises(MediaStorageCapacityExceeded):
                await guard.validate_projection(1)
            with pytest.raises(MediaStorageGuardError, match="already validated"):
                await guard.validate_projection(0)
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_guard_cannot_be_reentered() -> None:
    session = _GuardSession()
    guard = _guard(session)
    async with guard:
        await guard.validate_projection(0)
    with pytest.raises(MediaStorageGuardError, match="re-entered"):
        await guard.__aenter__()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "error"),
    [(True, TypeError), (1.5, TypeError), ("1", TypeError), (-1, ValueError)],
)
async def test_invalid_additional_bytes_never_measure_usage(
    size: object,
    error: type[Exception],
) -> None:
    session = _GuardSession()
    async with _guard(session) as guard:
        with pytest.raises(error):
            await guard.validate_projection(size)  # type: ignore[arg-type]
        assert not any("media_assets" in sql for sql in session.statements)
        await guard.validate_projection(0)


@pytest.mark.asyncio
async def test_pending_media_insert_before_validation_is_rejected() -> None:
    session = _GuardSession()
    with pytest.raises(MediaStorageGuardError, match="cannot precede"):
        async with _guard(session) as guard:
            session.new.append(MediaAsset(tenant_id=_GUARD_TENANT_ID))
            await guard.validate_projection(1)
    assert not any("media_assets" in sql for sql in session.statements)
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_exit_without_approved_projection_rolls_back() -> None:
    session = _GuardSession()
    with pytest.raises(MediaStorageGuardError, match="not approved"):
        async with _guard(session):
            pass
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_caller_failure_inside_guard_rolls_back_and_propagates() -> None:
    session = _GuardSession()
    with pytest.raises(RuntimeError, match="planted"):
        async with _guard(session) as guard:
            await guard.validate_projection(1)
            raise RuntimeError("planted failure after projection")
    assert session.rollbacks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_kwargs", "message"),
    [
        ({"actor_exists": False}, "actor user no longer exists"),
        ({"builder_exists": False}, "storage owner no longer exists"),
        ({"tenant_exists": False}, "subject tenant no longer exists"),
        ({"current_builder_id": None}, "owner changed"),
        ({"current_builder_id": UUID(int=7)}, "owner changed"),
    ],
)
async def test_lock_phase_failure_or_divergent_owner_rolls_back_before_media(
    session_kwargs: dict[str, Any],
    message: str,
) -> None:
    session = _GuardSession(**session_kwargs)
    guard = _guard(session)
    with pytest.raises(MediaStorageGuardError, match=message):
        async with guard:
            raise AssertionError("unreachable")
    assert session.rollbacks == 1
    assert not any("media_assets" in sql for sql in session.statements)
    with pytest.raises(MediaStorageGuardError, match="not active"):
        await guard.validate_projection(0)


# envio-midia-agente Group 2: the media -> intent binding, the union of the
# four "Quando enviar" topics (owner, 2026-11-08).
_BINDABLE = (
    "interesse_tratamento",
    "pedido_preco",
    "comparacao_tratamentos",
    "objecao_vou_pensar",
    "agendar",
    "detalhes_servicos",
    "pre_atendimento",
    "cultura_historia",
    "qualificacao_publico",
    "informacoes_profissionais",
    "pos_atendimento_duvida",
)


def test_trigger_intents_drop_duplicates_into_the_canonical_order() -> None:
    assert validate_trigger_intents([]) == []
    assert validate_trigger_intents(
        ["detalhes_servicos", "interesse_tratamento", "detalhes_servicos"]
    ) == ["interesse_tratamento", "detalhes_servicos"]
    assert validate_trigger_intents(
        ["pos_atendimento_duvida", "pedido_preco", "pedido_preco"]
    ) == ["pedido_preco", "pos_atendimento_duvida"]
    # Every bindable intent at once, in reverse: the canonical order back.
    assert validate_trigger_intents(list(reversed(_BINDABLE))) == list(_BINDABLE)
    assert media_assets.MEDIA_TRIGGER_INTENTS_MAX_ITEMS == len(_BINDABLE) == 11


def test_trigger_intents_accept_the_scheduling_request_topic() -> None:
    # The owner opened "Solicitação de Agendamento" to media: agendar binds,
    # with pre_atendimento, its topic's other intent.
    assert validate_trigger_intents(["pre_atendimento", "agendar"]) == [
        "agendar",
        "pre_atendimento",
    ]


@pytest.mark.parametrize(
    "value",
    [
        # Scheduling flows that act on an appointment, and the out-of-hours
        # handoff: never bindable.
        "reagendar",
        "cancelar",
        "confirmar_presenca",
        "disponibilidade_horarios",
        "fora_do_horario",
        # A complaint always hands off.
        "reclamacao",
        # "Outras" is implied, never stored.
        "saudacao",
        "faq",
        # Official, but not informative.
        "pedido_atendente_humano",
        "cobranca_renegociacao",
        # Exact names only: no folding, no trimming.
        "FAQS",
        "Pedido_Preco",
        " pedido_preco",
        "pedido_preco ",
        "",
        "pedido_preco,agendar",
        "inexistente",
        None,
        1,
        ["pedido_preco"],
    ],
)
def test_trigger_intents_refuse_any_name_outside_the_allowlist(value: object) -> None:
    with pytest.raises(MediaAssetDomainError) as caught:
        validate_trigger_intents(["pedido_preco", value])
    assert caught.value.code == "media_trigger_intent_unknown"


def test_trigger_intents_refuse_more_entries_than_bindable_intents() -> None:
    with pytest.raises(MediaAssetDomainError) as caught:
        validate_trigger_intents([*_BINDABLE, "pedido_preco"])
    assert caught.value.code == "media_metadata_invalid"
    # The count is checked before any name, so an over-long list of unknown
    # names is still the size error.
    with pytest.raises(MediaAssetDomainError) as caught:
        validate_trigger_intents(["reagendar"] * 12)
    assert caught.value.code == "media_metadata_invalid"


def test_media_assets_map_trigger_intents_as_a_non_null_text_array() -> None:
    column = MediaAsset.__table__.c.trigger_intents
    assert column.nullable is False
    assert str(column.type.compile(dialect=postgresql.dialect())) == "TEXT[]"
    assert column.server_default is not None
    assert str(column.server_default.arg) == "'{}'"
    # Every ORM insert names the column with a fresh empty list.
    assert column.default is not None
    first, second = column.default.arg(None), column.default.arg(None)
    assert first == second == [] and first is not second
