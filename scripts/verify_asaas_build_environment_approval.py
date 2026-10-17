#!/usr/bin/env python3
"""Verify run-bound admin UI evidence before requesting the build OIDC token."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import re
import struct
import sys
import stat
import tempfile
import zlib
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_SCRIPT_DIR = Path(__file__).resolve().parent
_PREFLIGHT_PATH = _SCRIPT_DIR / "asaas_build_release_preflight.py"
_SPEC = importlib.util.spec_from_file_location(
    "asaas_build_release_preflight", _PREFLIGHT_PATH
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("ASAAS build preflight module is unavailable")
_PREFLIGHT = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault(_SPEC.name, _PREFLIGHT)
_SPEC.loader.exec_module(_PREFLIGHT)

API_ROOT = _PREFLIGHT.API_ROOT
API_PREFIX = _PREFLIGHT.API_PREFIX
REPOSITORY = _PREFLIGHT.REPOSITORY
REPOSITORY_ID = _PREFLIGHT.REPOSITORY_ID
OWNER_ID = _PREFLIGHT.OWNER_ID
RELEASE_REF = _PREFLIGHT.RELEASE_REF
WORKFLOW_PATH = _PREFLIGHT.WORKFLOW_PATH
ENVIRONMENT = "asaas-build"
COMMENT_SCHEMA = "neurostack.asaas_build_ui_attestation.v6"
UI_APPROVAL_JOB_NAME = "Verify run-bound admin UI evidence before OIDC (no token)"
BUILD_JOB_NAME = "Build and sign after second asaas-build approval"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 20 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
MAX_GROUP2_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_GROUP2_UNPACKED_BYTES = 128 * 1024 * 1024
# Independent AWS MCP DescribeRegions response pinned from the separately
# collected read-only Group 2 source artifact. A changed region catalog or
# opt-in status requires a fresh independent observation and reviewed pin.
PINNED_GROUP2_REGIONS_RESPONSE_SHA256 = (
    "f4e52f7420e339d831af2555ef035e75316cc01351296330ed68da7f119a6e00"
)
MAX_GROUP2_FILES = 2048
MAX_APPROVAL_COMMENT_BYTES = 2048
MAX_BUILD_JOB_AGE = timedelta(minutes=2)
MAX_UI_CHECK_AGE = timedelta(minutes=5)
MAX_DECODED_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_DECODED_PNG_TEXT_BYTES = 4 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ATTACHMENT_PATH = re.compile(
    r"/user-attachments/assets/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_MANIFEST_ATTACHMENT_PATH = re.compile(
    r"/user-attachments/files/[1-9][0-9]*/[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.json"
)
_BUNDLE_ATTACHMENT_PATH = re.compile(
    r"/user-attachments/files/[1-9][0-9]*/[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.zip"
)


class ApprovalGateError(ValueError):
    """A workflow or human approval does not match the expected release."""


class _PngTextBudget:
    def __init__(self) -> None:
        self.used = 0

    @property
    def remaining(self) -> int:
        return MAX_DECODED_PNG_TEXT_BYTES - self.used

    def consume(self, size: int) -> None:
        _require(
            size >= 0 and size <= self.remaining,
            "approval_evidence_png_invalid",
        )
        self.used += size


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class _GitHubAttachmentRedirect(HTTPRedirectHandler):
    """Follow only GitHub's attachment redirect to its image CDN."""

    def __init__(self):
        super().__init__()
        self.redirect_count = 0

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        self.redirect_count += 1
        _require(self.redirect_count <= 3, "approval_evidence_redirect_limit")
        _require(
            _is_allowed_attachment_redirect(new_url), "approval_evidence_ref_invalid"
        )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ApprovalGateError(reason)


def _is_allowed_attachment_redirect(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
        return False
    if hostname == "github.com":
        return (
            _ATTACHMENT_PATH.fullmatch(parsed.path) is not None
            or _MANIFEST_ATTACHMENT_PATH.fullmatch(parsed.path) is not None
            or _BUNDLE_ATTACHMENT_PATH.fullmatch(parsed.path) is not None
        )
    return hostname == "githubusercontent.com" or hostname.endswith(
        ".githubusercontent.com"
    )


def _validate_attachment_ref(url: str) -> None:
    parsed = urlsplit(url)
    _require(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and _ATTACHMENT_PATH.fullmatch(parsed.path) is not None
        and not parsed.query
        and not parsed.fragment,
        "approval_evidence_ref_invalid",
    )


def _validate_manifest_ref(url: str) -> None:
    parsed = urlsplit(url)
    _require(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and _MANIFEST_ATTACHMENT_PATH.fullmatch(parsed.path) is not None
        and not parsed.query
        and not parsed.fragment,
        "approval_manifest_ref_invalid",
    )


def _validate_group2_bundle_ref(url: str) -> None:
    parsed = urlsplit(url)
    _require(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and _BUNDLE_ATTACHMENT_PATH.fullmatch(parsed.path) is not None
        and not parsed.query
        and not parsed.fragment,
        "approval_group2_bundle_ref_invalid",
    )


def _paeth_predictor(left: int, above: int, upper_left: int) -> int:
    prediction = left + above - upper_left
    distances = (
        abs(prediction - left),
        abs(prediction - above),
        abs(prediction - upper_left),
    )
    return (left, above, upper_left)[distances.index(min(distances))]


def _validate_png_zlib_stream(
    compressed: bytes,
    max_output_bytes: int,
    *,
    error_reason: str = "approval_evidence_png_invalid",
) -> bytes:
    decoder = zlib.decompressobj()
    decoded = bytearray()
    pending = compressed
    try:
        while pending:
            output = decoder.decompress(pending, max_output_bytes + 1 - len(decoded))
            decoded.extend(output)
            _require(
                len(decoded) <= max_output_bytes,
                error_reason,
            )
            pending = decoder.unconsumed_tail
            if decoder.eof:
                break
    except zlib.error:
        raise ApprovalGateError(error_reason) from None
    _require(
        decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
        error_reason,
    )
    return bytes(decoded)


def _validate_png_text(
    text: bytes, budget: _PngTextBudget, *, utf8: bool = False
) -> None:
    budget.consume(len(text))
    _require(b"\0" not in text, "approval_evidence_png_invalid")
    if utf8:
        try:
            text.decode("utf-8")
        except UnicodeDecodeError:
            raise ApprovalGateError("approval_evidence_png_invalid") from None


def _validate_png(raw: bytes) -> None:
    _require(
        isinstance(raw, bytes)
        and 33 <= len(raw) <= MAX_EVIDENCE_BYTES
        and raw.startswith(PNG_SIGNATURE),
        "approval_evidence_png_invalid",
    )
    offset = len(PNG_SIGNATURE)
    seen_ihdr = False
    seen_plte = False
    seen_idat = False
    seen_chunks: set[bytes] = set()
    suggested_palette_names: set[bytes] = set()
    text_budget = _PngTextBudget()
    palette_entries = 0
    transparency_seen = False
    idat_ended = False
    saw_iend = False
    idat_chunks: list[bytes] = []
    width = height = bit_depth = color_type = interlace = 0
    channels_by_color_type = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    allowed_bit_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    while offset < len(raw):
        _require(offset + 12 <= len(raw), "approval_evidence_png_invalid")
        length = struct.unpack_from(">I", raw, offset)[0]
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        chunk_end = data_end + 4
        _require(
            chunk_end <= len(raw)
            and len(chunk_type) == 4
            and all((65 <= char <= 90) or (97 <= char <= 122) for char in chunk_type)
            # PNG reserves the third chunk-name bit; it must be uppercase.
            and 65 <= chunk_type[2] <= 90,
            "approval_evidence_png_invalid",
        )
        data = raw[data_start:data_end]
        expected_crc = struct.unpack_from(">I", raw, data_end)[0]
        _require(
            zlib.crc32(chunk_type + data) & 0xFFFFFFFF == expected_crc,
            "approval_evidence_png_crc_invalid",
        )
        if not seen_ihdr:
            _require(
                chunk_type == b"IHDR" and length == 13,
                "approval_evidence_png_invalid",
            )
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", data)
            )
            _require(
                compression == 0
                and filtering == 0
                and interlace in (0, 1)
                and color_type in allowed_bit_depths
                and bit_depth in allowed_bit_depths[color_type],
                "approval_evidence_png_invalid",
            )
            seen_ihdr = True
        elif chunk_type == b"IHDR":
            _require(False, "approval_evidence_png_invalid")
        elif chunk_type == b"PLTE":
            _require(
                not seen_plte
                and not seen_idat
                and b"bKGD" not in seen_chunks
                and color_type in (2, 3, 6)
                and 3 <= length <= 768
                and length % 3 == 0
                and (color_type != 3 or length // 3 <= 2**bit_depth),
                "approval_evidence_png_invalid",
            )
            palette_entries = length // 3
            seen_plte = True
        elif chunk_type == b"IDAT":
            _require(not idat_ended, "approval_evidence_png_invalid")
            seen_idat = True
            idat_chunks.append(data)
        elif chunk_type == b"IEND":
            _require(
                length == 0 and seen_idat and offset + 12 == len(raw),
                "approval_evidence_png_invalid",
            )
            saw_iend = True
            offset = chunk_end
            break
        else:
            if seen_idat:
                idat_ended = True
            is_critical = (chunk_type[0] & 0x20) == 0
            _require(
                not is_critical or chunk_type in (b"PLTE",),
                "approval_evidence_png_invalid",
            )
            # Validate the standard ancillary chunks used by image encoders.
            # Unknown ancillary chunks are ignored as the PNG specification
            # requires, while known chunks with unsupported encodings fail closed.
            if chunk_type in {
                b"cHRM",
                b"gAMA",
                b"iCCP",
                b"sBIT",
                b"sRGB",
                b"bKGD",
                b"hIST",
                b"tRNS",
                b"pHYs",
                b"sPLT",
                b"tIME",
                b"tEXt",
                b"zTXt",
                b"iTXt",
            }:
                repeatable = chunk_type in {b"sPLT", b"tEXt", b"zTXt", b"iTXt"}
                _require(
                    repeatable or chunk_type not in seen_chunks,
                    "approval_evidence_png_invalid",
                )
                seen_chunks.add(chunk_type)
                before_idat_only = chunk_type in {
                    b"cHRM",
                    b"gAMA",
                    b"iCCP",
                    b"sBIT",
                    b"sRGB",
                    b"bKGD",
                    b"hIST",
                    b"tRNS",
                    b"pHYs",
                    b"sPLT",
                }
                _require(
                    not (before_idat_only and seen_idat),
                    "approval_evidence_png_invalid",
                )
                if chunk_type == b"cHRM":
                    _require(
                        length == 32 and not seen_plte, "approval_evidence_png_invalid"
                    )
                elif chunk_type == b"gAMA":
                    _require(
                        length == 4
                        and struct.unpack(">I", data)[0] != 0
                        and not seen_plte,
                        "approval_evidence_png_invalid",
                    )
                elif chunk_type == b"iCCP":
                    separator = data.find(b"\0")
                    _require(
                        1 <= separator <= 79
                        and separator + 2 < length
                        and data[separator + 1] == 0,
                        "approval_evidence_png_invalid",
                    )
                    profile = _validate_png_zlib_stream(
                        data[separator + 2 :], MAX_DECODED_PNG_TEXT_BYTES
                    )
                    _require(
                        128 <= len(profile),
                        "approval_evidence_png_invalid",
                    )
                    _require(not seen_plte, "approval_evidence_png_invalid")
                elif chunk_type == b"sBIT":
                    expected = {0: 1, 2: 3, 3: 3, 4: 2, 6: 4}[color_type]
                    _require(length == expected, "approval_evidence_png_invalid")
                    maximum = 8 if color_type == 3 else bit_depth
                    _require(
                        all(1 <= value <= maximum for value in data),
                        "approval_evidence_png_invalid",
                    )
                elif chunk_type == b"sRGB":
                    _require(
                        length == 1
                        and data[0] <= 3
                        and b"iCCP" not in seen_chunks
                        and not seen_plte,
                        "approval_evidence_png_invalid",
                    )
                elif chunk_type == b"bKGD":
                    expected = {0: 2, 2: 6, 3: 1, 4: 2, 6: 6}[color_type]
                    _require(
                        length == expected and (color_type != 3 or seen_plte),
                        "approval_evidence_png_invalid",
                    )
                    if color_type == 3:
                        _require(
                            data[0] < palette_entries, "approval_evidence_png_invalid"
                        )
                elif chunk_type == b"hIST":
                    _require(
                        seen_plte and length == palette_entries * 2,
                        "approval_evidence_png_invalid",
                    )
                elif chunk_type == b"tRNS":
                    _require(color_type in (0, 2, 3), "approval_evidence_png_invalid")
                    expected = {0: 2, 2: 6, 3: None}[color_type]
                    _require(
                        expected is None
                        and seen_plte
                        and 1 <= length <= palette_entries
                        or expected is not None
                        and length == expected,
                        "approval_evidence_png_invalid",
                    )
                elif chunk_type == b"pHYs":
                    _require(
                        length == 9 and data[8] <= 1, "approval_evidence_png_invalid"
                    )
                elif chunk_type == b"sPLT":
                    separator = data.find(b"\0")
                    _require(
                        1 <= separator <= 79 and separator + 2 < length,
                        "approval_evidence_png_invalid",
                    )
                    _require(
                        data[:separator] not in suggested_palette_names,
                        "approval_evidence_png_invalid",
                    )
                    suggested_palette_names.add(data[:separator])
                    sample_depth = data[separator + 1]
                    entry_size = (
                        10 if sample_depth == 8 else 18 if sample_depth == 16 else 0
                    )
                    _require(
                        entry_size > 0 and (length - separator - 2) % entry_size == 0,
                        "approval_evidence_png_invalid",
                    )
                elif chunk_type == b"tIME":
                    _require(length == 7, "approval_evidence_png_invalid")
                    try:
                        datetime(
                            *struct.unpack(">HBBBBB", data)[:3],
                            hour=data[4],
                            minute=data[5],
                            second=min(data[6], 59),
                        )
                        _require(data[6] <= 60, "approval_evidence_png_invalid")
                    except ValueError:
                        raise ApprovalGateError(
                            "approval_evidence_png_invalid"
                        ) from None
                elif chunk_type in {b"tEXt", b"zTXt", b"iTXt"}:
                    separator = data.find(b"\0")
                    _require(1 <= separator <= 79, "approval_evidence_png_invalid")
                    keyword = data[:separator]
                    _require(
                        all(32 <= byte <= 126 or 161 <= byte <= 255 for byte in keyword)
                        and not keyword.startswith(b" ")
                        and not keyword.endswith(b" ")
                        and b"  " not in keyword,
                        "approval_evidence_png_invalid",
                    )
                    text_budget.consume(len(keyword))
                    if chunk_type == b"zTXt":
                        _require(
                            separator + 2 < length and data[separator + 1] == 0,
                            "approval_evidence_png_invalid",
                        )
                        text = _validate_png_zlib_stream(
                            data[separator + 2 :], text_budget.remaining
                        )
                        _validate_png_text(text, text_budget)
                    elif chunk_type == b"iTXt":
                        cursor = separator + 1
                        _require(
                            cursor + 2 <= length and data[cursor] <= 1,
                            "approval_evidence_png_invalid",
                        )
                        compressed_text, method = data[cursor], data[cursor + 1]
                        _require(
                            not compressed_text or method == 0,
                            "approval_evidence_png_invalid",
                        )
                        cursor += 2
                        language_end = data.find(b"\0", cursor)
                        _require(
                            language_end >= cursor, "approval_evidence_png_invalid"
                        )
                        text_budget.consume(language_end - cursor)
                        translated_end = data.find(b"\0", language_end + 1)
                        _require(
                            translated_end >= language_end + 1,
                            "approval_evidence_png_invalid",
                        )
                        translated_keyword = data[language_end + 1 : translated_end]
                        _validate_png_text(translated_keyword, text_budget, utf8=True)
                        payload = data[translated_end + 1 :]
                        if compressed_text:
                            text = _validate_png_zlib_stream(
                                payload, text_budget.remaining
                            )
                        else:
                            text = payload
                        _validate_png_text(text, text_budget, utf8=True)
                    else:
                        _validate_png_text(data[separator + 1 :], text_budget)
        if seen_idat and chunk_type != b"IDAT":
            idat_ended = True
        offset = chunk_end
    _require(
        seen_ihdr
        and seen_idat
        and saw_iend
        and offset == len(raw)
        and (color_type != 3 or seen_plte),
        "approval_evidence_png_invalid",
    )
    _require(
        0 < width <= 8192 and 0 < height <= 8192 and width * height <= 40_000_000,
        "approval_evidence_png_dimensions_invalid",
    )
    bits_per_pixel = channels_by_color_type[color_type] * bit_depth
    passes = (
        ((0, 0, 1, 1),)
        if interlace == 0
        else (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
    )
    expected_decoded_bytes = 0
    row_sizes: list[tuple[int, int, int]] = []
    for start_x, start_y, step_x, step_y in passes:
        pass_width = max(0, (width - start_x + step_x - 1) // step_x)
        pass_height = max(0, (height - start_y + step_y - 1) // step_y)
        row_bytes = (pass_width * bits_per_pixel + 7) // 8
        expected_decoded_bytes += pass_height * (row_bytes + 1)
        if pass_width and pass_height:
            row_sizes.append((pass_height, pass_width, row_bytes))
    _require(
        0 < expected_decoded_bytes <= MAX_DECODED_EVIDENCE_BYTES,
        "approval_evidence_png_dimensions_invalid",
    )
    decoded = _validate_png_zlib_stream(
        b"".join(idat_chunks),
        expected_decoded_bytes,
        error_reason="approval_evidence_png_data_invalid",
    )
    _require(
        len(decoded) == expected_decoded_bytes,
        "approval_evidence_png_data_invalid",
    )
    cursor = 0
    for rows, pass_width, row_bytes in row_sizes:
        bytes_per_pixel = max(1, (bits_per_pixel + 7) // 8)
        previous = bytearray(row_bytes)
        for _ in range(rows):
            filter_type = decoded[cursor]
            _require(filter_type <= 4, "approval_evidence_png_data_invalid")
            cursor += 1
            row = bytearray(decoded[cursor : cursor + row_bytes])
            for index in range(row_bytes):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                above = previous[index]
                upper_left = (
                    previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                )
                if filter_type == 1:
                    predictor = left
                elif filter_type == 2:
                    predictor = above
                elif filter_type == 3:
                    predictor = (left + above) // 2
                elif filter_type == 4:
                    predictor = _paeth_predictor(left, above, upper_left)
                else:
                    predictor = 0
                row[index] = (row[index] + predictor) & 0xFF
            if color_type == 3:
                mask = (1 << bit_depth) - 1
                for pixel in range(pass_width):
                    bit_offset = pixel * bit_depth
                    shift = 8 - bit_depth - bit_offset % 8
                    palette_index = (row[bit_offset // 8] >> shift) & mask
                    _require(
                        palette_index < palette_entries,
                        "approval_evidence_png_data_invalid",
                    )
            previous = row
            cursor += row_bytes
    _require(cursor == len(decoded), "approval_evidence_png_data_invalid")


def _fetch_github_attachment(url: str) -> bytes:
    _validate_attachment_ref(url)
    request = Request(
        url,
        headers={"Accept": "image/png", "Cache-Control": "no-cache"},
        method="GET",
    )
    try:
        with build_opener(_GitHubAttachmentRedirect()).open(
            request, timeout=15
        ) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            _require(
                status == 200,
                "approval_evidence_download_failed",
            )
            _require(
                _is_allowed_attachment_redirect(response.geturl()),
                "approval_evidence_ref_invalid",
            )
            _require(
                response.headers.get_content_type() == "image/png",
                "approval_evidence_media_type_invalid",
            )
            raw = response.read(MAX_EVIDENCE_BYTES + 1)
            _require(
                len(raw) <= MAX_EVIDENCE_BYTES,
                "approval_evidence_too_large",
            )
            _validate_png(raw)
            return raw
    except ApprovalGateError:
        raise
    except (HTTPError, URLError, OSError):
        raise ApprovalGateError("approval_evidence_download_failed") from None


def _fetch_github_manifest(url: str) -> bytes:
    _validate_manifest_ref(url)
    request = Request(
        url,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        method="GET",
    )
    try:
        with build_opener(_GitHubAttachmentRedirect()).open(
            request, timeout=15
        ) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            _require(status == 200, "approval_manifest_download_failed")
            _require(
                _is_allowed_attachment_redirect(response.geturl()),
                "approval_manifest_ref_invalid",
            )
            _require(
                response.headers.get_content_type() == "application/json",
                "approval_manifest_media_type_invalid",
            )
            raw = response.read(MAX_MANIFEST_BYTES + 1)
            _require(
                len(raw) <= MAX_MANIFEST_BYTES,
                "approval_manifest_too_large",
            )
            return raw
    except ApprovalGateError:
        raise
    except (HTTPError, URLError, OSError):
        raise ApprovalGateError("approval_manifest_download_failed") from None


def _fetch_group2_bundle(url: str) -> bytes:
    _validate_group2_bundle_ref(url)
    request = Request(
        url,
        headers={"Accept": "application/zip", "Cache-Control": "no-cache"},
        method="GET",
    )
    try:
        with build_opener(_GitHubAttachmentRedirect()).open(
            request, timeout=30
        ) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            _require(status == 200, "approval_group2_bundle_download_failed")
            _require(
                _is_allowed_attachment_redirect(response.geturl()),
                "approval_group2_bundle_ref_invalid",
            )
            _require(
                response.headers.get_content_type()
                in {"application/zip", "application/octet-stream"},
                "approval_group2_bundle_media_type_invalid",
            )
            raw = response.read(MAX_GROUP2_BUNDLE_BYTES + 1)
            _require(
                len(raw) <= MAX_GROUP2_BUNDLE_BYTES,
                "approval_group2_bundle_too_large",
            )
            return raw
    except ApprovalGateError:
        raise
    except (HTTPError, URLError, OSError):
        raise ApprovalGateError("approval_group2_bundle_download_failed") from None


def _validate_group2_bundle(raw: bytes, expected_sha256: str) -> dict[str, str]:
    _require(
        isinstance(raw, bytes) and 1 <= len(raw) <= MAX_GROUP2_BUNDLE_BYTES,
        "approval_group2_bundle_invalid",
    )
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    _require(
        actual_sha256 == expected_sha256,
        "approval_group2_bundle_digest_mismatch",
    )
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (OSError, zipfile.BadZipFile):
        raise ApprovalGateError("approval_group2_bundle_invalid") from None
    with archive:
        infos = archive.infolist()
        _require(0 < len(infos) <= MAX_GROUP2_FILES, "approval_group2_bundle_invalid")
        names: set[str] = set()
        total_size = 0
        snapshot_names = 0
        for info in infos:
            name = info.filename
            _require(
                isinstance(name, str)
                and name
                and "\\" not in name
                and not name.startswith("/")
                and not (len(name) >= 2 and name[1] == ":"),
                "approval_group2_bundle_path_invalid",
            )
            parts = name.rstrip("/").split("/")
            _require(
                all(part not in {"", ".", ".."} for part in parts),
                "approval_group2_bundle_path_invalid",
            )
            normalized = "/".join(parts)
            _require(
                normalized not in names,
                "approval_group2_bundle_duplicate_path",
            )
            names.add(normalized)
            _require(not info.flag_bits & 0x1, "approval_group2_bundle_encrypted")
            mode = (info.external_attr >> 16) & 0xFFFF
            _require(not stat.S_ISLNK(mode), "approval_group2_bundle_symlink_invalid")
            if info.is_dir():
                continue
            total_size += info.file_size
            _require(
                total_size <= MAX_GROUP2_UNPACKED_BYTES,
                "approval_group2_bundle_unpacked_too_large",
            )
            if normalized == "snapshot.json":
                snapshot_names += 1
        _require(snapshot_names == 1, "approval_group2_bundle_snapshot_missing")

        validator_path = _SCRIPT_DIR / "validate_asaas_producer_release_readbacks.py"
        validator_spec = importlib.util.spec_from_file_location(
            "asaas_group2_readback_validator", validator_path
        )
        if validator_spec is None or validator_spec.loader is None:
            raise ApprovalGateError("approval_group2_validator_unavailable") from None
        validator = importlib.util.module_from_spec(validator_spec)
        try:
            validator_spec.loader.exec_module(validator)
        except ImportError:
            raise ApprovalGateError("approval_group2_validator_unavailable") from None

        try:
            with tempfile.TemporaryDirectory(prefix="asaas-group2-") as temporary_root:
                root = Path(temporary_root)
                root.chmod(0o700)
                for info in infos:
                    if info.is_dir():
                        target = root.joinpath(*info.filename.rstrip("/").split("/"))
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                        target.chmod(0o700)
                        continue
                    target = root.joinpath(*info.filename.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.parent.chmod(0o700)
                    contents = archive.read(info)
                    _require(
                        len(contents) == info.file_size,
                        "approval_group2_bundle_invalid",
                    )
                    with target.open("xb") as handle:
                        handle.write(contents)
                    target.chmod(0o600)
                snapshot_path = root / "snapshot.json"
                snapshot = _loads_strict_json(
                    snapshot_path.read_bytes(),
                    reason="approval_group2_snapshot_invalid",
                )
                errors = validator.validate_snapshot(
                    snapshot,
                    evidence_root=root,
                    expected_describe_regions_response_sha256=(
                        PINNED_GROUP2_REGIONS_RESPONSE_SHA256
                    ),
                )
                _require(
                    not any(
                        error.endswith(
                            "describe_regions_artifact.document.response_sha256_pinned: mismatch"
                        )
                        for error in errors
                    ),
                    "approval_group2_regions_source_unpinned",
                )
                _require(not errors, "approval_group2_snapshot_invalid")
                snapshot_sha256 = validator.canonical_snapshot_readback_sha256(snapshot)
        except ApprovalGateError:
            raise
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
            raise ApprovalGateError("approval_group2_bundle_invalid") from None
    return {
        "bundle_sha256": actual_sha256,
        "snapshot_sha256": snapshot_sha256,
    }


def _api_get(path: str, token: str) -> dict | list:
    url = path if path.startswith("https://") else API_ROOT + path
    parsed = urlsplit(url)
    _require(
        parsed.scheme == "https"
        and parsed.netloc == "api.github.com"
        and (parsed.path == API_PREFIX or parsed.path.startswith(API_PREFIX + "/")),
        "github_api_url_invalid",
    )
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with build_opener(_NoRedirect).open(request, timeout=10) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            _require(status == 200, "github_api_readback_failed")
            final = urlsplit(response.geturl())
            _require(
                final.scheme == "https"
                and final.netloc == "api.github.com"
                and final.path == parsed.path,
                "github_api_redirect_invalid",
            )
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            _require(len(raw) <= MAX_RESPONSE_BYTES, "github_api_response_too_large")
            value = json.loads(raw)
            _require(isinstance(value, (dict, list)), "github_api_response_invalid")
            return value
    except ApprovalGateError:
        raise
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        raise ApprovalGateError("github_api_readback_failed") from None


def _canonical_timestamp(value: str) -> datetime:
    _require(
        isinstance(value, str)
        and re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
            value,
        )
        is not None,
        "approval_timestamp_invalid",
    )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise ApprovalGateError("approval_timestamp_invalid") from None
    _require(
        parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") == value,
        "approval_timestamp_invalid",
    )
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        _require(key not in value, "approval_comment_duplicate_json_key")
        value[key] = item
    return value


def _loads_strict_json(raw: bytes, *, reason: str) -> object:
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except ApprovalGateError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise ApprovalGateError(reason) from None


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError):
        raise ApprovalGateError("approval_manifest_invalid") from None


def _parse_approval_comment(
    comment: str,
    *,
    expected: dict[str, str],
) -> dict[str, object]:
    _require(
        isinstance(comment, str)
        and 1 <= len(comment.encode("utf-8")) <= MAX_APPROVAL_COMMENT_BYTES,
        "approval_comment_invalid",
    )
    raw = comment.encode("utf-8")
    values = _loads_strict_json(raw, reason="approval_comment_invalid")
    _require(isinstance(values, dict), "approval_comment_invalid")
    _require(_canonical_json(values) == raw, "approval_comment_noncanonical")
    _require("checked_at_utc" in values, "approval_checked_at_missing")
    required = {
        "schema_version",
        "run_id",
        "run_attempt",
        "sha",
        "ref",
        "workflow_path",
        "environment",
        "approval_gate",
        "checked_at_utc",
        "reviewer_id",
        "reviewer_login",
        "preflight_sha256",
        "ruleset_bypass",
        "environment_admin_bypass",
        "branch_policy",
        "checks",
        "manifest_ref",
        "manifest_sha256",
        "group2_bundle_ref",
        "group2_bundle_sha256",
        "screenshots",
        "evidence_ref",
        "evidence_label",
        "evidence_sha256",
    }
    _require(set(values) == required, "approval_comment_fields_invalid")
    _require(
        values["schema_version"] == COMMENT_SCHEMA, "approval_comment_schema_invalid"
    )
    for key, expected_value in expected.items():
        _require(values.get(key) == expected_value, f"approval_{key}_mismatch")
    _canonical_timestamp(values["checked_at_utc"])
    _require(values["ruleset_bypass"] == "empty", "ruleset_bypass_not_attested")
    _require(
        values["environment_admin_bypass"] == "disabled",
        "environment_admin_bypass_not_attested",
    )
    _require(values["branch_policy"] == "main-only", "branch_policy_not_attested")
    _require(
        re.fullmatch(r"[0-9a-f]{64}", values["preflight_sha256"]) is not None,
        "approval_preflight_digest_invalid",
    )
    _validate_manifest_ref(values["manifest_ref"])
    _validate_group2_bundle_ref(values["group2_bundle_ref"])
    _validate_attachment_ref(values["evidence_ref"])
    screenshots = values["screenshots"]
    labels = {"ruleset", "asaas-build", "asaas-production-deployment"}
    _require(
        isinstance(screenshots, dict) and set(screenshots) == labels,
        "approval_comment_screenshots_invalid",
    )
    refs, screenshot_digests = set(), set()
    for screenshot in screenshots.values():
        _require(
            isinstance(screenshot, dict) and set(screenshot) == {"ref", "sha256"},
            "approval_comment_screenshots_invalid",
        )
        _validate_attachment_ref(screenshot["ref"])
        _require(
            isinstance(screenshot["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", screenshot["sha256"]) is not None,
            "approval_comment_screenshots_invalid",
        )
        refs.add(screenshot["ref"])
        screenshot_digests.add(screenshot["sha256"])
    _require(
        len(refs) == len(labels) and len(screenshot_digests) == len(labels),
        "approval_comment_screenshots_invalid",
    )
    for key in ("manifest_sha256", "evidence_sha256", "group2_bundle_sha256"):
        _require(
            isinstance(values[key], str)
            and re.fullmatch(r"[0-9a-f]{64}", values[key]) is not None,
            f"approval_{key}_invalid",
        )
    _require(
        values["evidence_label"]
        in {"ruleset", "asaas-build", "asaas-production-deployment"},
        "approval_evidence_label_invalid",
    )
    return values


def _validate_ui_manifest(
    raw: bytes,
    *,
    expected_sha256: str,
    comment: dict[str, object],
) -> dict[str, object]:
    _require(
        hashlib.sha256(raw).hexdigest() == expected_sha256,
        "approval_manifest_digest_mismatch",
    )
    manifest = _loads_strict_json(raw, reason="approval_manifest_json_invalid")
    _require(isinstance(manifest, dict), "approval_manifest_invalid")
    _require(_canonical_json(manifest) == raw, "approval_manifest_noncanonical")
    required = {
        "schema_version",
        "run_id",
        "run_attempt",
        "workflow_path",
        "sha",
        "preflight_sha256",
        "ref",
        "environment",
        "approval_gate",
        "checked_at_utc",
        "group2_bundle_ref",
        "group2_bundle_sha256",
        "checks",
        "screenshots_sha256",
    }
    _require(set(manifest) == required, "approval_manifest_fields_invalid")
    _require(
        manifest["schema_version"] == "neurostack.asaas_build_ui_evidence_manifest.v4",
        "approval_manifest_schema_invalid",
    )
    for key in (
        "run_id",
        "run_attempt",
        "workflow_path",
        "sha",
        "preflight_sha256",
        "ref",
        "environment",
        "approval_gate",
        "checked_at_utc",
        "group2_bundle_ref",
        "group2_bundle_sha256",
    ):
        _require(
            manifest[key] == comment[key],
            f"approval_manifest_{key}_mismatch",
        )
    expected_checks = {
        "ruleset_bypass_empty": True,
        "asaas_build_admin_bypass_disabled": True,
        "asaas_build_branch_only_main": True,
        "asaas_production_admin_bypass_disabled": True,
        "asaas_production_branch_only_main": True,
    }
    _require(manifest["checks"] == expected_checks, "approval_manifest_checks_invalid")
    _require(comment["checks"] == expected_checks, "approval_comment_checks_invalid")
    screenshot_hashes = manifest["screenshots_sha256"]
    labels = {"ruleset", "asaas-build", "asaas-production-deployment"}
    _require(
        isinstance(screenshot_hashes, dict) and set(screenshot_hashes) == labels,
        "approval_manifest_screenshots_invalid",
    )
    hashes = list(screenshot_hashes.values())
    _require(
        all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in hashes
        )
        and len(set(hashes)) == len(labels),
        "approval_manifest_screenshots_invalid",
    )
    label = comment["evidence_label"]
    _require(
        screenshot_hashes[label] == comment["evidence_sha256"],
        "approval_evidence_label_digest_mismatch",
    )
    _require(
        {
            name: {
                "sha256": screenshot_hashes[name],
                "ref": comment["screenshots"][name]["ref"],
            }
            for name in labels
        }
        == comment["screenshots"],
        "approval_manifest_screenshots_mismatch",
    )
    _require(
        comment["screenshots"][label]["ref"] == comment["evidence_ref"]
        and comment["screenshots"][label]["sha256"] == comment["evidence_sha256"],
        "approval_evidence_label_digest_mismatch",
    )
    return manifest


def _verify_approval_package(
    comment: str,
    *,
    expected: dict[str, str],
    approval_gate: str,
    preflight_at: datetime,
    job_started_at: datetime,
    fetch_manifest,
    fetch_evidence,
    fetch_group2_bundle,
    expected_sha256: str = "",
    expected_checked_at_utc: str = "",
) -> dict[str, object]:
    fields = _parse_approval_comment(comment, expected=expected)
    _require(fields["approval_gate"] == approval_gate, "approval_gate_mismatch")
    checked_at = _canonical_timestamp(fields["checked_at_utc"])
    _require(
        checked_at <= job_started_at,
        f"{approval_gate}_approval_check_after_job_start",
    )
    _require(
        job_started_at - checked_at <= MAX_UI_CHECK_AGE,
        f"{approval_gate}_approval_check_stale",
    )
    _require(checked_at >= preflight_at, "approval_ui_check_before_preflight")
    if expected_checked_at_utc:
        _require(
            checked_at == _canonical_timestamp(expected_checked_at_utc),
            "approval_checked_at_changed_after_no_oidc_gate",
        )
    manifest_bytes = fetch_manifest(fields["manifest_ref"])
    manifest = _validate_ui_manifest(
        manifest_bytes,
        expected_sha256=fields["manifest_sha256"],
        comment=fields,
    )
    verified: dict[str, bytes] = {}
    for label, screenshot in fields["screenshots"].items():
        screenshot_bytes = fetch_evidence(screenshot["ref"])
        _validate_png(screenshot_bytes)
        _require(
            hashlib.sha256(screenshot_bytes).hexdigest() == screenshot["sha256"],
            "approval_evidence_digest_mismatch",
        )
        verified[label] = screenshot_bytes
    group2_bytes = fetch_group2_bundle(fields["group2_bundle_ref"])
    group2 = _validate_group2_bundle(group2_bytes, fields["group2_bundle_sha256"])
    selected = verified[fields["evidence_label"]]
    selected_sha256 = hashlib.sha256(selected).hexdigest()
    if expected_sha256:
        _require(
            re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
            "approval_expected_evidence_digest_invalid",
        )
        _require(
            selected_sha256 == expected_sha256,
            "approval_evidence_changed_after_no_oidc_gate",
        )
    return {
        "fields": fields,
        "manifest": manifest,
        "selected": selected,
        "selected_sha256": selected_sha256,
        "group2": group2,
    }


def _controls_snapshot(
    token: str,
    *,
    get_json,
    get_collection,
    backend_trust_digest: str,
    backend_trust_origin: str,
    trust_mode_value: str = "normal",
    backend_probe_digest: str = "",
) -> tuple[dict, dict]:
    def get_with_headers(url: str, api_token: str):
        return get_json(url, api_token), None

    repository = get_json(f"{API_ROOT}{API_PREFIX}", token)
    _require(
        isinstance(repository, dict)
        and repository.get("full_name") == REPOSITORY
        and repository.get("id") == REPOSITORY_ID,
        "repository_readback_mismatch",
    )
    owner = repository.get("owner")
    _require(
        isinstance(owner, dict) and owner.get("id") == OWNER_ID,
        "repository_owner_readback_mismatch",
    )
    _require(
        repository.get("default_branch") == "main", "repository_default_branch_mismatch"
    )
    rulesets = get_collection(
        f"{API_ROOT}{API_PREFIX}/rulesets?includes_parents=true&per_page=100&page=1",
        token,
    )
    ruleset = _PREFLIGHT.validate_ruleset(
        rulesets, token, github_get_fn=get_with_headers
    )
    environments = {
        name: _PREFLIGHT.validate_environment(
            name,
            token,
            github_get_fn=get_with_headers,
            github_collection_fn=get_collection,
        )
        for name in _PREFLIGHT.ENVIRONMENTS
    }
    digest = _PREFLIGHT.controls_digest(
        repository,
        ruleset,
        environments,
        backend_trust_digest,
        backend_trust_origin,
        trust_mode_value=trust_mode_value,
        backend_probe_digest=backend_probe_digest,
    )
    return {
        "repository": repository,
        "ruleset": ruleset,
        "environments": environments,
    }, {"digest": digest}


def _api_timestamp(value: str, *, field: str) -> datetime:
    _require(isinstance(value, str), f"{field}_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApprovalGateError(f"{field}_invalid") from None
    _require(parsed.tzinfo is not None, f"{field}_invalid")
    return parsed.astimezone(timezone.utc)


def verify_environment_approval(
    env: dict[str, str],
    *,
    get_json=None,
    get_collection=None,
    get_evidence=None,
    get_manifest=None,
    get_group2_bundle=None,
    now: datetime | None = None,
    phase: str = "build",
    expected_evidence_sha256: str = "",
    expected_checked_at_utc: str = "",
    expected_group2_bundle_sha256: str = "",
) -> dict[str, str]:
    get = _api_get if get_json is None else get_json
    collect = _PREFLIGHT.github_collection if get_collection is None else get_collection
    fetch_evidence = _fetch_github_attachment if get_evidence is None else get_evidence
    fetch_manifest = (
        _fetch_github_manifest
        if get_manifest is None and get_evidence is None
        else get_evidence if get_manifest is None else get_manifest
    )
    fetch_group2_bundle = (
        (_fetch_group2_bundle if get_evidence is None else get_evidence)
        if get_group2_bundle is None
        else get_group2_bundle
    )
    _require(phase in {"evidence", "build"}, "approval_phase_invalid")
    if phase == "build":
        expected_evidence_sha256 = expected_evidence_sha256 or env.get(
            "EXPECTED_APPROVAL_EVIDENCE_SHA256", ""
        )
        expected_checked_at_utc = expected_checked_at_utc or env.get(
            "EXPECTED_UI_CHECKED_AT_UTC", ""
        )
        expected_group2_bundle_sha256 = expected_group2_bundle_sha256 or env.get(
            "EXPECTED_GROUP2_BUNDLE_SHA256", ""
        )
    if phase == "build":
        _require(
            re.fullmatch(r"[0-9a-f]{64}", expected_evidence_sha256) is not None
            and bool(expected_checked_at_utc),
            "approval_no_oidc_outputs_missing",
        )
        _require(
            re.fullmatch(r"[0-9a-f]{64}", expected_group2_bundle_sha256) is not None,
            "approval_no_oidc_group2_output_missing",
        )
    token = env.get("GH_TOKEN", "")
    _require(bool(token), "github_read_token_unavailable")
    run_id = env.get("GITHUB_RUN_ID", "")
    _require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None, "actions_run_id_invalid")
    run_attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    _require(
        re.fullmatch(r"[1-9][0-9]*", run_attempt) is not None,
        "actions_run_attempt_invalid",
    )
    sha = env.get("GITHUB_SHA", "")
    _require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "actions_run_sha_invalid")
    _require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "repository_name_mismatch")
    _require(
        env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID), "repository_id_mismatch"
    )
    _require(
        env.get("GITHUB_REPOSITORY_OWNER_ID") == str(OWNER_ID),
        "repository_owner_id_mismatch",
    )
    _require(env.get("GITHUB_REF") == RELEASE_REF, "workflow_ref_mismatch")
    expected_workflow_ref = f"{REPOSITORY}/{WORKFLOW_PATH}@{RELEASE_REF}"
    _require(
        env.get("GITHUB_WORKFLOW_REF") == expected_workflow_ref,
        "workflow_identity_mismatch",
    )
    mode = _PREFLIGHT.trust_mode(env)
    _require(
        env.get("ASAAS_PREFLIGHT_BUILD_TRUST_MODE", "") == mode,
        "build_trust_mode_changed_after_preflight",
    )
    preflight_digest = env.get("ASAAS_PREFLIGHT_READBACK_SHA256", "")
    _require(
        re.fullmatch(r"[0-9a-f]{64}", preflight_digest) is not None,
        "approval_preflight_digest_invalid",
    )
    preflight_at_raw = env.get("ASAAS_PREFLIGHT_OBSERVED_AT", "")
    preflight_at = _canonical_timestamp(preflight_at_raw)
    backend_trust_preflight_digest = env.get("ASAAS_PREFLIGHT_BACKEND_TRUST_SHA256", "")
    backend_probe_preflight_digest = env.get(
        "ASAAS_PREFLIGHT_BACKEND_TRUST_PROBE_SHA256", ""
    )
    if mode == "normal":
        _require(
            re.fullmatch(r"[0-9a-f]{64}", backend_trust_preflight_digest) is not None,
            "backend_trust_preflight_digest_invalid",
        )
        _require(not backend_probe_preflight_digest, "backend_trust_probe_unexpected")
    else:
        _require(not backend_trust_preflight_digest, "backend_trust_digest_unexpected")
        _require(
            re.fullmatch(r"[0-9a-f]{64}", backend_probe_preflight_digest) is not None,
            "backend_trust_probe_digest_invalid",
        )
    backend_trust_preflight_origin = env.get("ASAAS_PREFLIGHT_BACKEND_ORIGIN", "")
    _require(
        re.fullmatch(r"https://[a-z0-9.-]+", backend_trust_preflight_origin)
        is not None,
        "backend_trust_origin_invalid",
    )
    _require(
        _PREFLIGHT.backend_trust_origin(env) == backend_trust_preflight_origin,
        "backend_trust_origin_changed_after_preflight",
    )

    run_path = f"{API_PREFIX}/actions/runs/{run_id}"
    run = get(f"{API_ROOT}{run_path}", token)
    _require(
        isinstance(run, dict)
        and type(run.get("id")) is int
        and run["id"] == int(run_id),
        "actions_run_id_mismatch",
    )
    _require(
        run.get("event") == "workflow_dispatch"
        and run.get("status") == "in_progress"
        and run.get("run_attempt") == int(run_attempt)
        and run.get("head_branch") == "main"
        and run.get("head_sha") == sha
        and run.get("path") == f"{WORKFLOW_PATH}@{RELEASE_REF}",
        "actions_run_context_mismatch",
    )
    exact_ref = get(f"{API_ROOT}{API_PREFIX}/git/ref/heads/main", token)
    exact_ref_object = exact_ref.get("object") if isinstance(exact_ref, dict) else None
    _require(
        isinstance(exact_ref, dict)
        and exact_ref.get("ref") == RELEASE_REF
        and isinstance(exact_ref_object, dict)
        and exact_ref_object.get("type") == "commit"
        and exact_ref_object.get("sha") == run.get("head_sha"),
        "actions_run_exact_ref_readback_mismatch",
    )
    if mode == "bootstrap":
        expected_title = (
            f"ASAAS producer build bootstrap {env.get('INPUT_OPERATION_ID', '')}"
        )
        _require(
            run.get("display_title") == expected_title,
            "bootstrap_run_title_mismatch",
        )
        _require(
            env.get("GITHUB_RUN_ATTEMPT") == "1",
            "bootstrap_run_attempt_must_be_first",
        )
    repo = run.get("repository")
    head_repo = run.get("head_repository")
    _require(
        isinstance(repo, dict)
        and repo.get("full_name") == REPOSITORY
        and repo.get("id") == REPOSITORY_ID
        and isinstance(head_repo, dict)
        and head_repo.get("id") == REPOSITORY_ID,
        "actions_run_repository_mismatch",
    )
    actor = run.get("actor")
    _require(
        isinstance(actor, dict)
        and type(actor.get("id")) is int
        and actor["id"] > 0
        and isinstance(actor.get("login"), str)
        and bool(actor["login"].strip()),
        "actions_run_actor_missing",
    )
    actor_ids = {actor["id"]}
    triggering_actor = run.get("triggering_actor")
    _require(
        isinstance(triggering_actor, dict)
        and type(triggering_actor.get("id")) is int
        and triggering_actor["id"] > 0
        and isinstance(triggering_actor.get("login"), str)
        and bool(triggering_actor["login"].strip()),
        "actions_run_triggering_actor_missing_or_invalid",
    )
    actor_ids.add(triggering_actor["id"])
    jobs = collect(
        f"{API_ROOT}{run_path}/jobs?per_page=100&page=1",
        token,
        list_field="jobs",
    )
    preflight_jobs = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("name") == "preflight"
        and job.get("head_sha") == sha
    ]
    ui_approval_jobs = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("head_sha") == sha
        and job.get("name")
        in {
            UI_APPROVAL_JOB_NAME,
            f"verify-ui-approval / {UI_APPROVAL_JOB_NAME}",
        }
    ]
    build_jobs = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("head_sha") == sha
        and job.get("name")
        in {
            BUILD_JOB_NAME,
            f"build-attestation / {BUILD_JOB_NAME}",
        }
    ]
    _require(
        len(preflight_jobs) == 1
        and preflight_jobs[0].get("status") == "completed"
        and preflight_jobs[0].get("conclusion") == "success",
        "actions_preflight_job_not_successful",
    )
    if mode == "bootstrap":
        try:
            _PREFLIGHT.validate_bootstrap_operation_unused(
                token,
                env.get("INPUT_OPERATION_ID", ""),
                run_id,
                github_collection_fn=collect,
            )
        except _PREFLIGHT.GateError as exc:
            raise ApprovalGateError(str(exc)) from None
    current_time = datetime.now(timezone.utc) if now is None else now
    if phase == "evidence":
        _require(
            len(ui_approval_jobs) == 1
            and ui_approval_jobs[0].get("status") == "in_progress",
            "actions_ui_evidence_job_not_current",
        )
        _require(
            len(build_jobs) == 1
            and build_jobs[0].get("status") in {"queued", "waiting"}
            and not build_jobs[0].get("started_at"),
            "actions_build_job_started_before_ui_evidence_gate",
        )
        ui_job_started_at = _api_timestamp(
            ui_approval_jobs[0].get("started_at"),
            field="actions_ui_evidence_job_timestamp",
        )
        gate_job_started_at = ui_job_started_at
    else:
        _require(
            len(ui_approval_jobs) == 1
            and ui_approval_jobs[0].get("status") == "completed"
            and ui_approval_jobs[0].get("conclusion") == "success",
            "actions_ui_evidence_job_not_successful",
        )
        _require(
            len(build_jobs) == 1 and build_jobs[0].get("status") == "in_progress",
            "actions_build_job_not_current",
        )
        ui_job_started_at = _api_timestamp(
            ui_approval_jobs[0].get("started_at"),
            field="actions_ui_evidence_job_timestamp",
        )
        ui_job_completed_at = _api_timestamp(
            ui_approval_jobs[0].get("completed_at"),
            field="actions_ui_evidence_job_timestamp",
        )
        _require(
            ui_job_started_at <= ui_job_completed_at,
            "actions_ui_evidence_job_timestamp_invalid",
        )
        gate_job_started_at = _api_timestamp(
            build_jobs[0].get("started_at"), field="actions_build_job_timestamp"
        )
        _require(
            ui_job_completed_at <= gate_job_started_at,
            "actions_build_job_started_before_ui_evidence_completion",
        )
    _require(
        preflight_at <= gate_job_started_at <= current_time + timedelta(seconds=30),
        "actions_gate_job_timestamp_invalid",
    )
    _require(
        current_time - gate_job_started_at <= MAX_BUILD_JOB_AGE,
        "actions_gate_job_not_current",
    )

    # Re-read every API-visible control after the environment approval. The
    # reviewer comment must name the exact digest produced by preflight.
    controls, digest_info = _controls_snapshot(
        token,
        get_json=get,
        get_collection=collect,
        backend_trust_digest=backend_trust_preflight_digest,
        backend_trust_origin=backend_trust_preflight_origin,
        trust_mode_value=mode,
        backend_probe_digest=backend_probe_preflight_digest,
    )
    _require(
        digest_info["digest"] == preflight_digest,
        "github_controls_changed_after_preflight",
    )
    build_environment = controls["environments"].get(ENVIRONMENT)
    _require(isinstance(build_environment, dict), "environment_readback_missing")
    reviewers = build_environment.get("reviewers")
    _require(
        isinstance(reviewers, list)
        and len(reviewers) == 1
        and reviewers[0].get("type") == "User",
        "environment_reviewer_identity_invalid",
    )
    required_reviewer = reviewers[0]

    approvals = get(f"{API_ROOT}{run_path}/approvals", token)
    approval_observed_at = datetime.now(timezone.utc) if now is None else now
    _require(isinstance(approvals, list), "actions_approval_history_invalid")
    matching = [
        entry
        for entry in approvals
        if isinstance(entry, dict)
        and isinstance(entry.get("environments"), list)
        and any(
            isinstance(item, dict) and item.get("name") == ENVIRONMENT
            for item in entry["environments"]
        )
    ]
    expected_approval_count = 1 if phase == "evidence" else 2
    _require(
        len(matching) == expected_approval_count,
        "environment_approval_count_mismatch",
    )
    _require(
        all(entry.get("state") == "approved" for entry in matching),
        "environment_approval_not_approved",
    )
    required_reviewer = build_environment["reviewers"][0]
    users = [entry.get("user") for entry in matching]
    _require(
        all(
            isinstance(user, dict)
            and type(user.get("id")) is int
            and user.get("id") == required_reviewer.get("id")
            and isinstance(user.get("login"), str)
            and user.get("login") == required_reviewer.get("login")
            for user in users
        ),
        "environment_approval_reviewer_mismatch",
    )
    user = users[0]
    assert isinstance(user, dict)
    _require(
        all(candidate["id"] not in actor_ids for candidate in users),
        "environment_approval_not_independent",
    )
    permission = get(
        f"{API_ROOT}{API_PREFIX}/collaborators/{user['login']}/permission", token
    )
    _require(
        isinstance(permission, dict) and permission.get("permission") == "admin",
        "environment_reviewer_is_not_repository_admin",
    )
    approval_comments = [
        entry.get("comment")
        for entry in matching
        if isinstance(entry.get("comment"), str) and entry.get("comment")
    ]
    _require(
        len(approval_comments) == expected_approval_count
        and len(set(approval_comments)) == expected_approval_count,
        "approval_phase_comments_missing_reused_or_mismatched",
    )
    comment_expected = {
        "run_id": run_id,
        "run_attempt": run_attempt,
        "sha": sha,
        "ref": RELEASE_REF,
        "workflow_path": WORKFLOW_PATH,
        "environment": ENVIRONMENT,
        "reviewer_id": str(user["id"]),
        "reviewer_login": user["login"],
        "preflight_sha256": preflight_digest,
    }
    comments_by_gate: dict[str, str] = {}
    for approval_comment in approval_comments:
        parsed = _parse_approval_comment(approval_comment, expected=comment_expected)
        approval_gate = parsed["approval_gate"]
        _require(
            approval_gate in {"evidence", "build"}
            and approval_gate not in comments_by_gate,
            "approval_phase_comments_missing_reused_or_mismatched",
        )
        comments_by_gate[approval_gate] = approval_comment
    required_gates = {"evidence"} if phase == "evidence" else {"evidence", "build"}
    _require(
        set(comments_by_gate) == required_gates,
        "approval_phase_comments_missing_reused_or_mismatched",
    )
    package_evidence = _verify_approval_package(
        comments_by_gate["evidence"],
        expected={**comment_expected, "approval_gate": "evidence"},
        approval_gate="evidence",
        preflight_at=preflight_at,
        job_started_at=ui_job_started_at,
        fetch_manifest=fetch_manifest,
        fetch_evidence=fetch_evidence,
        fetch_group2_bundle=fetch_group2_bundle,
        expected_sha256=expected_evidence_sha256 if phase == "build" else "",
        expected_checked_at_utc=expected_checked_at_utc if phase == "build" else "",
    )
    if phase == "build":
        _require(
            package_evidence["group2"]["bundle_sha256"]
            == expected_group2_bundle_sha256,
            "approval_group2_bundle_changed_after_no_oidc_gate",
        )
    packages = {"evidence": package_evidence}
    if phase == "build":
        package_build = _verify_approval_package(
            comments_by_gate["build"],
            expected={**comment_expected, "approval_gate": "build"},
            approval_gate="build",
            preflight_at=preflight_at,
            job_started_at=gate_job_started_at,
            fetch_manifest=fetch_manifest,
            fetch_evidence=fetch_evidence,
            fetch_group2_bundle=fetch_group2_bundle,
        )
        evidence_fields = package_evidence["fields"]
        build_fields = package_build["fields"]
        _require(
            evidence_fields["checked_at_utc"] != build_fields["checked_at_utc"]
            and evidence_fields["manifest_sha256"] != build_fields["manifest_sha256"]
            and evidence_fields["manifest_ref"] != build_fields["manifest_ref"]
            and {
                item["ref"] for item in evidence_fields["screenshots"].values()
            }.isdisjoint(
                {item["ref"] for item in build_fields["screenshots"].values()}
            ),
            "approval_package_reused_between_gates",
        )
        _require(
            _canonical_timestamp(build_fields["checked_at_utc"]) >= ui_job_completed_at,
            "build_approval_ui_check_before_evidence_job_completed",
        )
        _require(
            {
                item["sha256"] for item in evidence_fields["screenshots"].values()
            }.isdisjoint(
                {item["sha256"] for item in build_fields["screenshots"].values()}
            ),
            "approval_screenshot_bytes_reused_between_gates",
        )
        _require(
            package_evidence["group2"] == package_build["group2"]
            and evidence_fields["group2_bundle_ref"]
            == build_fields["group2_bundle_ref"],
            "approval_group2_bundle_changed_between_gates",
        )
        _require(
            ui_job_started_at < gate_job_started_at,
            "actions_build_job_must_start_after_evidence_job",
        )
        packages["build"] = package_build
    selected_package = packages[phase]
    comment_fields = selected_package["fields"]
    manifest = selected_package["manifest"]
    evidence_bytes = selected_package["selected"]
    actual_evidence_sha256 = selected_package["selected_sha256"]
    observed_backend_trust_digest = ""
    observed_backend_probe_digest = ""
    try:
        if mode == "normal":
            _backend_trust, observed_backend_trust_digest = (
                _PREFLIGHT.fetch_backend_trust_readback(
                    env, expected_digest=backend_trust_preflight_digest
                )
            )
            _require(
                observed_backend_trust_digest == backend_trust_preflight_digest,
                "backend_trust_changed_after_preflight",
            )
        else:
            observed_backend_probe_digest = (
                _PREFLIGHT.probe_backend_trust_readback_unavailable(
                    env, expected_digest=backend_probe_preflight_digest
                )
            )
    except _PREFLIGHT.GateError as exc:
        raise ApprovalGateError(str(exc)) from None
    return {
        "schema": "asaas_admin_ui_approval.v2",
        "run_id": run_id,
        "workflow_path": WORKFLOW_PATH,
        "sha": sha,
        "ref": RELEASE_REF,
        "environment": ENVIRONMENT,
        "reviewer_id": str(user["id"]),
        "reviewer_login": user["login"],
        "reviewer_permission": "admin",
        "preflight_sha256": preflight_digest,
        "build_trust_mode": mode,
        "backend_trust_readback_sha256": observed_backend_trust_digest,
        "backend_trust_probe_sha256": observed_backend_probe_digest,
        "backend_trust_origin": backend_trust_preflight_origin,
        "evidence_ref": comment_fields["evidence_ref"],
        "evidence_sha256": actual_evidence_sha256,
        "evidence_verified": "true",
        "evidence_size_bytes": str(len(evidence_bytes)),
        "manifest_ref": comment_fields["manifest_ref"],
        "manifest_sha256": comment_fields["manifest_sha256"],
        "group2_bundle_ref": comment_fields["group2_bundle_ref"],
        "group2_bundle_sha256": selected_package["group2"]["bundle_sha256"],
        "group2_snapshot_sha256": selected_package["group2"]["snapshot_sha256"],
        "screenshots_sha256": json.dumps(
            manifest["screenshots_sha256"], sort_keys=True, separators=(",", ":")
        ),
        "screenshots_refs": json.dumps(
            {
                label: value["ref"]
                for label, value in comment_fields["screenshots"].items()
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        "approval_gate": "github_environment_required_reviewer_approved",
        "approval_sequence": (
            "first_no_oidc_gate" if phase == "evidence" else "second_oidc_gate"
        ),
        "ui_checked_at_utc": comment_fields["checked_at_utc"],
        "approval_observed_at_utc": approval_observed_at.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "approval_observation_source": (
            "verifier clock when GitHub Actions returned the approved state; "
            "this is not the reviewer click time"
        ),
    }


def _write_evidence(path: Path, evidence: dict[str, str]) -> None:
    encoded = json.dumps(
        evidence,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--phase", choices=("evidence", "build"), default="build")
    parser.add_argument("--expected-evidence-sha256", default="")
    parser.add_argument("--expected-checked-at-utc", default="")
    parser.add_argument("--expected-group2-bundle-sha256", default="")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        evidence = verify_environment_approval(
            dict(os.environ),
            phase=args.phase,
            expected_evidence_sha256=args.expected_evidence_sha256,
            expected_checked_at_utc=args.expected_checked_at_utc,
            expected_group2_bundle_sha256=args.expected_group2_bundle_sha256,
        )
        _write_evidence(args.evidence_output, evidence)
        if args.github_output is not None:
            with args.github_output.open("a", encoding="utf-8") as output:
                output.write(
                    f"approval_evidence_sha256={evidence['evidence_sha256']}\n"
                )
                output.write(f"ui_checked_at_utc={evidence['ui_checked_at_utc']}\n")
                output.write(
                    f"group2_bundle_sha256={evidence['group2_bundle_sha256']}\n"
                )
                output.write(
                    f"group2_snapshot_sha256={evidence['group2_snapshot_sha256']}\n"
                )
    except ApprovalGateError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    except (OSError, TypeError, ValueError):
        print("blocked: administrator approval could not be verified", file=sys.stderr)
        return 1
    phase_summary = (
        "This protected verification job has no OIDC permission. The build job needs "
        "a second approval for the same environment and validates it before requesting OIDC."
        if args.phase == "evidence"
        else "The second required approval for this environment and run-bound evidence "
        "were verified before the first OIDC request."
    )
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(
            "## Administrator UI approval verified\n\n"
            f"- Run: `{evidence['run_id']}`\n"
            f"- SHA: `{evidence['sha']}`\n"
            f"- Reviewer: `{evidence['reviewer_login']}` (repository admin)\n"
            f"- Environment: `{evidence['environment']}`\n"
            f"- Backend trust SHA-256: `{evidence['backend_trust_readback_sha256']}`\n"
            f"- Backend origin: `{evidence['backend_trust_origin']}`\n"
            f"- Evidence: `{evidence['evidence_ref']}`\n"
            f"- Evidence SHA-256: `{evidence['evidence_sha256']}`\n"
            f"- Manifest SHA-256: `{evidence['manifest_sha256']}`\n"
            f"- Group 2 bundle SHA-256: `{evidence['group2_bundle_sha256']}`\n"
            f"- Group 2 snapshot SHA-256: `{evidence['group2_snapshot_sha256']}`\n"
            f"- Screenshot SHA-256 values: `{evidence['screenshots_sha256']}`\n"
            f"- Evidence PNG verified: `{evidence['evidence_verified']}` ({evidence['evidence_size_bytes']} bytes)\n"
            f"- Approval gate: `{evidence['approval_gate']}`\n"
            f"- UI attestation checked at: `{evidence['ui_checked_at_utc']}`\n"
            f"- Approval record observed: `{evidence['approval_observed_at_utc']}`\n"
            f"- Observation source: `{evidence['approval_observation_source']}`\n"
            "The human checked time is run-bound evidence, not a GitHub click time.\n"
            f"{phase_summary}\n"
        )
    print("Run-bound independent administrator UI approval verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
