#!/usr/bin/env python3
"""Verify run-bound admin UI evidence before requesting the build OIDC token."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import struct
import sys
import zlib
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
COMMENT_PREFIX = "ASAAS-ADMIN-UI-ATTESTATION/2"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
MAX_BUILD_JOB_AGE = timedelta(minutes=2)
MAX_DECODED_EVIDENCE_BYTES = 128 * 1024 * 1024
MAX_DECODED_PNG_TEXT_BYTES = 4 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ATTACHMENT_PATH = re.compile(
    r"/user-attachments/assets/[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
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
        return _ATTACHMENT_PATH.fullmatch(parsed.path) is not None
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
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
        )
        is not None,
        "approval_timestamp_invalid",
    )
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise ApprovalGateError("approval_timestamp_invalid") from None
    return parsed


def _parse_approval_comment(
    comment: str,
    *,
    expected: dict[str, str],
) -> dict[str, str]:
    _require(
        isinstance(comment, str) and 1 <= len(comment) <= 2048 and "\n" not in comment,
        "approval_comment_invalid",
    )
    parts = comment.split(" ")
    _require(
        parts[0] == COMMENT_PREFIX and all(parts) and " ".join(parts) == comment,
        "approval_comment_invalid",
    )
    values: dict[str, str] = {}
    for item in parts[1:]:
        _require("=" in item, "approval_comment_invalid")
        key, value = item.split("=", 1)
        _require(key and value and key not in values, "approval_comment_invalid")
        values[key] = value
    required = {
        "run_id",
        "sha",
        "ref",
        "workflow_path",
        "environment",
        "reviewer_id",
        "reviewer_login",
        "preflight_sha256",
        "ruleset_bypass",
        "environment_admin_bypass",
        "branch_policy",
        "evidence_ref",
        "evidence_sha256",
    }
    _require(set(values) == required, "approval_comment_fields_invalid")
    for key, expected_value in expected.items():
        _require(values.get(key) == expected_value, f"approval_{key}_mismatch")
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
    _require(isinstance(values["evidence_ref"], str), "approval_evidence_ref_invalid")
    _validate_attachment_ref(values["evidence_ref"])
    _require(
        re.fullmatch(r"[0-9a-f]{64}", values["evidence_sha256"]) is not None,
        "approval_evidence_digest_invalid",
    )
    return values


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


def _api_timestamp(value: str) -> datetime:
    _require(isinstance(value, str), "actions_build_job_timestamp_missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApprovalGateError("actions_build_job_timestamp_invalid") from None
    _require(parsed.tzinfo is not None, "actions_build_job_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def verify_environment_approval(
    env: dict[str, str],
    *,
    get_json=None,
    get_collection=None,
    get_evidence=None,
    now: datetime | None = None,
) -> dict[str, str]:
    get = _api_get if get_json is None else get_json
    collect = _PREFLIGHT.github_collection if get_collection is None else get_collection
    fetch_evidence = _fetch_github_attachment if get_evidence is None else get_evidence
    token = env.get("GH_TOKEN", "")
    _require(bool(token), "github_read_token_unavailable")
    run_id = env.get("GITHUB_RUN_ID", "")
    _require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None, "actions_run_id_invalid")
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
        and run.get("head_branch") == "main"
        and run.get("head_sha") == sha
        and run.get("path") == f"{WORKFLOW_PATH}@{RELEASE_REF}",
        "actions_run_context_mismatch",
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
    build_jobs = [
        job
        for job in jobs
        if isinstance(job, dict)
        and job.get("head_sha") == sha
        and job.get("name")
        in {
            "Build and sign after live protections and environment approval",
            "build-attestation / Build and sign after live protections and environment approval",
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
    _require(
        len(build_jobs) == 1 and build_jobs[0].get("status") == "in_progress",
        "actions_build_job_not_current",
    )
    current_time = datetime.now(timezone.utc) if now is None else now
    build_job_started_at = _api_timestamp(build_jobs[0].get("started_at"))
    _require(
        preflight_at <= build_job_started_at <= current_time + timedelta(seconds=30),
        "actions_build_job_timestamp_invalid",
    )
    _require(
        current_time - build_job_started_at <= MAX_BUILD_JOB_AGE,
        "actions_build_job_not_current",
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
    _require(len(matching) == 1, "environment_approval_count_mismatch")
    approval = matching[0]
    _require(approval.get("state") == "approved", "environment_approval_not_approved")
    user = approval.get("user")
    _require(
        isinstance(user, dict)
        and type(user.get("id")) is int
        and user.get("id") == required_reviewer.get("id")
        and isinstance(user.get("login"), str)
        and user.get("login") == required_reviewer.get("login"),
        "environment_approval_reviewer_mismatch",
    )
    _require(user["id"] not in actor_ids, "environment_approval_not_independent")
    permission = get(
        f"{API_ROOT}{API_PREFIX}/collaborators/{user['login']}/permission", token
    )
    _require(
        isinstance(permission, dict) and permission.get("permission") == "admin",
        "environment_reviewer_is_not_repository_admin",
    )
    comment_expected = {
        "run_id": run_id,
        "sha": sha,
        "ref": RELEASE_REF,
        "workflow_path": WORKFLOW_PATH,
        "environment": ENVIRONMENT,
        "reviewer_id": str(user["id"]),
        "reviewer_login": user["login"],
        "preflight_sha256": preflight_digest,
    }
    comment_fields = _parse_approval_comment(
        approval.get("comment"),
        expected=comment_expected,
    )
    evidence_bytes = fetch_evidence(comment_fields["evidence_ref"])
    _validate_png(evidence_bytes)
    actual_evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    _require(
        actual_evidence_sha256 == comment_fields["evidence_sha256"],
        "approval_evidence_digest_mismatch",
    )
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
            observed_backend_probe_digest = _PREFLIGHT.probe_backend_trust_absent(
                env, expected_digest=backend_probe_preflight_digest
            )
    except _PREFLIGHT.GateError as exc:
        raise ApprovalGateError(str(exc)) from None
    return {
        "schema": "asaas_admin_ui_approval.v1",
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
        "approval_gate": "github_environment_required_reviewer_approved",
        "approval_gate_satisfied_at_utc": build_job_started_at.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "approval_observed_at_utc": current_time.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
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
    args = parser.parse_args()
    try:
        evidence = verify_environment_approval(dict(os.environ))
        _write_evidence(args.evidence_output, evidence)
    except ApprovalGateError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    except (OSError, TypeError, ValueError):
        print("blocked: administrator approval could not be verified", file=sys.stderr)
        return 1
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
            f"- Evidence PNG verified: `{evidence['evidence_verified']}` ({evidence['evidence_size_bytes']} bytes)\n"
            f"- Approval gate: `{evidence['approval_gate']}`\n"
            f"- Approval gate satisfied: `{evidence['approval_gate_satisfied_at_utc']}`\n"
            f"- Approval record observed: `{evidence['approval_observed_at_utc']}`\n"
            "The remaining steps may request OIDC only after this verification.\n"
        )
    print("Run-bound independent administrator UI approval verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
