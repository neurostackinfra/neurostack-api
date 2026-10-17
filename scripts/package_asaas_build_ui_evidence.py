#!/usr/bin/env python3
"""Privately package reviewed ASAAS build UI screenshots and emit approval JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCREENSHOTS = {
    "ruleset": "ruleset.png",
    "asaas-build": "asaas-build.png",
    "asaas-production-deployment": "asaas-production-deployment.png",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024
MAX_IMAGE_DIMENSION = 8192
MAX_IMAGE_PIXELS = 16_000_000
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024
COLOR_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
VALID_BIT_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_png(source_value: str, packaged_at: datetime) -> tuple[bytes, datetime]:
    source = Path(source_value)
    if source.is_symlink() or not source.is_file():
        raise ValueError("each screenshot must be a regular, non-symlink PNG file")
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError("each screenshot must be a regular, non-symlink PNG file")
        modified_at = datetime.fromtimestamp(source_stat.st_mtime, timezone.utc)
        if (
            modified_at < packaged_at - timedelta(minutes=5)
            or modified_at > packaged_at
        ):
            raise ValueError("screenshot mtime is stale or in the future")
        if (
            source_stat.st_size <= len(PNG_SIGNATURE)
            or source_stat.st_size > MAX_SCREENSHOT_BYTES
        ):
            raise ValueError("screenshot size is outside the accepted range")
        with os.fdopen(descriptor, "rb", closefd=False) as input_file:
            contents = input_file.read(MAX_SCREENSHOT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(contents) <= len(PNG_SIGNATURE) or len(contents) > MAX_SCREENSHOT_BYTES:
        raise ValueError("screenshot size is outside the accepted range")
    _validate_png(contents)
    return contents, modified_at


def _validate_png(contents: bytes) -> None:
    if not contents.startswith(PNG_SIGNATURE):
        raise ValueError("screenshot is not a PNG file")
    offset = len(PNG_SIGNATURE)
    width = height = bit_depth = color_type = interlace = None
    palette = None
    idat_parts: list[bytes] = []
    idat_ended = False
    saw_iend = False
    saw_plte = False
    while offset < len(contents):
        if offset + 12 > len(contents):
            raise ValueError("screenshot PNG structure is invalid")
        length = struct.unpack(">I", contents[offset : offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(contents):
            raise ValueError("screenshot PNG structure is invalid")
        chunk_type = contents[offset + 4 : offset + 8]
        chunk_data = contents[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", contents[offset + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise ValueError("screenshot PNG checksum is invalid")
        if width is None:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("screenshot PNG header is invalid")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", chunk_data)
            if (
                width == 0
                or height == 0
                or width > MAX_IMAGE_DIMENSION
                or height > MAX_IMAGE_DIMENSION
                or width * height > MAX_IMAGE_PIXELS
                or color_type not in COLOR_CHANNELS
                or bit_depth not in VALID_BIT_DEPTHS.get(color_type, set())
                or compression != 0
                or filter_method != 0
                or interlace not in {0, 1}
            ):
                raise ValueError("screenshot PNG header values are invalid")
        elif chunk_type == b"IHDR":
            raise ValueError("screenshot PNG header is duplicated")
        elif chunk_type == b"PLTE":
            if saw_plte or idat_parts or length == 0 or length > 768 or length % 3:
                raise ValueError("screenshot PNG palette is invalid")
            if color_type in {0, 4}:
                raise ValueError("screenshot PNG palette is not allowed")
            if color_type == 3 and length // 3 > (1 << bit_depth):
                raise ValueError("screenshot PNG palette is too large")
            palette = chunk_data
            saw_plte = True
        elif chunk_type == b"IDAT":
            if idat_ended:
                raise ValueError("screenshot PNG IDAT chunks are not consecutive")
            idat_parts.append(chunk_data)
        else:
            if idat_parts:
                idat_ended = True
            if chunk_type == b"IEND":
                if length != 0 or chunk_end != len(contents):
                    raise ValueError("screenshot PNG end marker is invalid")
                saw_iend = True
                break
            if chunk_type[0] & 0x20 == 0:
                raise ValueError("screenshot PNG contains an unknown critical chunk")
        offset = chunk_end
    if width is None or not idat_parts or not saw_iend:
        raise ValueError("screenshot PNG is incomplete")
    if color_type == 3 and palette is None:
        raise ValueError("screenshot PNG indexed color has no palette")
    expected_rows: list[int] = []
    passes = ADAM7_PASSES if interlace else ((0, 0, 1, 1),)
    bits_per_pixel = COLOR_CHANNELS[color_type] * bit_depth
    for start_x, start_y, step_x, step_y in passes:
        pass_width = max(0, (width - start_x + step_x - 1) // step_x)
        pass_height = max(0, (height - start_y + step_y - 1) // step_y)
        if pass_width and pass_height:
            row_bytes = (pass_width * bits_per_pixel + 7) // 8
            expected_rows.extend([row_bytes] * pass_height)
    expected_size = sum(row_bytes + 1 for row_bytes in expected_rows)
    if expected_size > MAX_DECOMPRESSED_BYTES:
        raise ValueError("screenshot PNG decompressed size exceeds the limit")
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(b"".join(idat_parts), MAX_DECOMPRESSED_BYTES + 1)
    except zlib.error:
        raise ValueError("screenshot PNG image data is invalid") from None
    if (
        not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
        or len(decoded) != expected_size
    ):
        raise ValueError(
            "screenshot PNG image data is truncated or has invalid scanlines"
        )
    cursor = 0
    for row_bytes in expected_rows:
        if decoded[cursor] > 4:
            raise ValueError("screenshot PNG scanline filter is invalid")
        cursor += row_bytes + 1


def _copy_png(contents: bytes, destination: Path) -> str:
    output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(output_fd, "wb", closefd=False) as output_file:
            output_file.write(contents)
            output_file.flush()
            os.fsync(output_fd)
    finally:
        os.close(output_fd)
    os.chmod(destination, 0o600)
    return hashlib.sha256(contents).hexdigest()


def _private_run_directory(
    run_id: str, attempt: str, approval_gate: str = "evidence"
) -> Path:
    home = Path.home().resolve()
    repository = _repository_root()
    root = (
        home
        / ".local/share/neurostack/asaas-build-ui-evidence/neurostackinfra-neurostack-api"
    )
    destination = root / f"run-{run_id}-attempt-{attempt}-gate-{approval_gate}"
    if root == repository or repository in root.parents or root in repository.parents:
        raise ValueError("private evidence directory must be outside the repository")
    current = home
    for component in (*root.relative_to(home).parts, destination.name):
        current = current / component
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        directory = current
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("private evidence path contains a non-directory component")
        if home not in directory.resolve().parents:
            raise ValueError("private evidence path escapes the user home directory")
        os.chmod(directory, 0o700)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("private evidence directory already contains files")
    return destination


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def package(args: argparse.Namespace) -> tuple[dict[str, object], Path]:
    if not re.fullmatch(r"[1-9][0-9]*", args.run_id):
        raise ValueError("run ID must be a positive decimal integer")
    if not re.fullmatch(r"[1-9][0-9]*", args.run_attempt):
        raise ValueError("run attempt must be a positive decimal integer")
    if not re.fullmatch(r"[0-9a-f]{40}", args.sha):
        raise ValueError("SHA must be 40 lowercase hexadecimal characters")
    preflight_sha256 = getattr(args, "preflight_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", preflight_sha256):
        raise ValueError(
            "preflight SHA-256 must be 64 lowercase hexadecimal characters"
        )
    approval_gate = getattr(args, "approval_gate", "evidence")
    if approval_gate not in {"evidence", "build"}:
        raise ValueError("approval gate must be evidence or build")
    group2_bundle_ref = getattr(args, "group2_bundle_ref", "")
    group2_bundle_sha256 = getattr(args, "group2_bundle_sha256", "")
    if not re.fullmatch(
        r"https://github\.com/user-attachments/files/[1-9][0-9]*/[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.zip",
        group2_bundle_ref,
    ):
        raise ValueError("Group 2 bundle must use the exact GitHub zip attachment URL")
    if not re.fullmatch(r"[0-9a-f]{64}", group2_bundle_sha256):
        raise ValueError("Group 2 bundle SHA-256 must be lowercase hexadecimal")
    evidence_label = getattr(args, "evidence_label", "ruleset")
    if evidence_label not in SCREENSHOTS:
        raise ValueError("evidence label must name a packaged screenshot")
    packaged_at = datetime.now(timezone.utc)
    repository = _repository_root()
    source_bytes: dict[str, bytes] = {}
    source_mtimes: dict[str, datetime] = {}
    for label in SCREENSHOTS:
        source = Path(getattr(args, label.replace("-", "_")))
        source_resolved = source.resolve()
        if repository in source_resolved.parents or source_resolved == repository:
            raise ValueError("screenshots must be sourced from outside the repository")
        source_bytes[label], source_mtimes[label] = _read_png(str(source), packaged_at)
    checked_at = (
        min(source_mtimes.values())
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    source_digests = {
        label: hashlib.sha256(contents).hexdigest()
        for label, contents in source_bytes.items()
    }
    if len(set(source_digests.values())) != len(SCREENSHOTS):
        raise ValueError("each UI screenshot must have a distinct SHA-256 digest")
    output = _private_run_directory(args.run_id, args.run_attempt, approval_gate)
    screenshot_digests = {
        label: _copy_png(source_bytes[label], output / filename)
        for label, filename in SCREENSHOTS.items()
    }
    if screenshot_digests != source_digests:
        raise ValueError("copied screenshot digest changed")
    metadata = {
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "sha": args.sha,
        "preflight_sha256": preflight_sha256,
        "ref": "refs/heads/main",
        "environment": "asaas-build",
        "approval_gate": approval_gate,
        "checked_at_utc": checked_at,
        "group2_bundle_ref": group2_bundle_ref,
        "group2_bundle_sha256": group2_bundle_sha256,
    }
    checks = {
        "ruleset_bypass_empty": True,
        "asaas_build_admin_bypass_disabled": True,
        "asaas_build_branch_only_main": True,
        "asaas_production_admin_bypass_disabled": True,
        "asaas_production_branch_only_main": True,
    }
    manifest = {
        "schema_version": "neurostack.asaas_build_ui_evidence_manifest.v4",
        **metadata,
        "checks": checks,
        "screenshots_sha256": screenshot_digests,
    }
    manifest_bytes = _canonical_json(manifest)
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    manifest_path = output / "manifest.json"
    with manifest_path.open("xb") as manifest_file:
        manifest_file.write(manifest_bytes)
        manifest_file.flush()
        os.fsync(manifest_file.fileno())
    os.chmod(manifest_path, 0o600)

    comment = {
        "schema_version": "neurostack.asaas_build_ui_attestation.v6",
        **metadata,
        "reviewer_id": "<github-user-id>",
        "reviewer_login": "<github-login>",
        "preflight_sha256": preflight_sha256,
        "ruleset_bypass": "empty",
        "environment_admin_bypass": "disabled",
        "branch_policy": "main-only",
        "checks": checks,
        "manifest_ref": "https://github.com/user-attachments/files/<id>/manifest.json",
        "manifest_sha256": manifest_digest,
        "screenshots": {
            label: {
                "ref": "https://github.com/user-attachments/assets/<uuid>",
                "sha256": screenshot_digests[label],
            }
            for label in SCREENSHOTS
        },
        "evidence_ref": "https://github.com/user-attachments/assets/<uuid>",
        "evidence_label": evidence_label,
        "evidence_sha256": screenshot_digests[evidence_label],
    }
    if len(_canonical_json(comment)) > 2048:
        raise ValueError("approval comment exceeds the 2048-byte workflow limit")
    return comment, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument(
        "--approval-gate", choices=("evidence", "build"), default="evidence"
    )
    parser.add_argument("--sha", required=True)
    parser.add_argument("--preflight-sha256", required=True)
    parser.add_argument("--group2-bundle-ref", required=True)
    parser.add_argument("--group2-bundle-sha256", required=True)
    parser.add_argument(
        "--evidence-label", choices=tuple(SCREENSHOTS), default="ruleset"
    )
    parser.add_argument("--ruleset", required=True)
    parser.add_argument("--asaas-build", required=True)
    parser.add_argument("--asaas-production-deployment", required=True)
    args = parser.parse_args()
    try:
        comment, output = package(args)
        print(f"Private evidence directory: {output}")
        print(
            f"Manifest SHA-256: {hashlib.sha256((output / 'manifest.json').read_bytes()).hexdigest()}"
        )
        print(
            "Upload manifest.json and all three screenshots as GitHub user attachments."
        )
        print(
            "Fill manifest_ref, screenshots.*.ref, evidence_ref, reviewer_id/login, and preflight_sha256 before approval:"
        )
        print(json.dumps(comment, sort_keys=True, separators=(",", ":")))
        for label, digest in comment["checks"].items():
            if not digest:
                raise ValueError(f"review check failed: {label}")
        manifest = json.loads((output / "manifest.json").read_bytes())
        for label, digest in manifest["screenshots_sha256"].items():
            print(f"Screenshot {label} SHA-256: {digest}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
