from __future__ import annotations

import importlib.util
import json
import os
import stat
import struct
import sys
import zlib
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/package_asaas_build_ui_evidence.py"
)
_SPEC = importlib.util.spec_from_file_location("asaas_ui_evidence_packer_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
packer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(packer)

_VERIFIER_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts/verify_asaas_build_environment_approval.py"
)
_VERIFIER_SPEC = importlib.util.spec_from_file_location(
    "asaas_ui_evidence_timestamp_verifier_test", _VERIFIER_SCRIPT
)
assert _VERIFIER_SPEC is not None and _VERIFIER_SPEC.loader is not None
verifier = importlib.util.module_from_spec(_VERIFIER_SPEC)
sys.modules[_VERIFIER_SPEC.name] = verifier
_VERIFIER_SPEC.loader.exec_module(verifier)


def _png(
    seed: bytes = b"x",
    *,
    width: int = 1,
    height: int = 1,
    bit_depth: int = 8,
    color_type: int = 6,
    compression: int = 0,
    filter_method: int = 0,
    interlace: int = 0,
    image_data: bytes | None = None,
) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        color_type,
        compression,
        filter_method,
        interlace,
    )
    pixel = zlib.compress(
        image_data if image_data is not None else b"\x00\x00\x00\x00\x00"
    )
    return (
        packer.PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"tEXt", b"label\x00" + seed)
        + chunk(b"IDAT", pixel)
        + chunk(b"IEND", b"")
    )


def _args(tmp_path: Path) -> Namespace:
    screenshot_dir = tmp_path / "screenshots"
    screenshot_dir.mkdir()
    files = {}
    for key in (
        "ruleset",
        "asaas_build",
        "asaas_production_deployment",
    ):
        path = screenshot_dir / f"{key}.png"
        path.write_bytes(_png(key.encode("ascii")))
        files[key] = str(path)
    return Namespace(
        run_id="7123456789",
        run_attempt="2",
        sha="a" * 40,
        **files,
    )


def test_packer_creates_private_run_manifest_and_approval_json(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repository)
    args = _args(tmp_path)

    before = datetime.now(timezone.utc)
    source_mtimes = [
        datetime.fromtimestamp(
            Path(getattr(args, key.replace("-", "_"))).stat().st_mtime, timezone.utc
        )
        for key in packer.SCREENSHOTS
    ]
    comment, output = packer.package(args)
    after = datetime.now(timezone.utc)

    expected = (
        home / ".local/share/neurostack/asaas-build-ui-evidence/"
        "neurostackinfra-neurostack-api/run-7123456789-attempt-2"
    )
    assert output == expected
    assert repository not in output.parents
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    manifest_path = output / "manifest.json"
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    manifest = json.loads(manifest_path.read_text())
    assert len(packer._canonical_json(comment)) <= 2048
    assert (
        manifest["schema_version"] == "neurostack.asaas_build_ui_evidence_manifest.v2"
    )
    assert (
        packer.hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        == comment["manifest_sha256"]
    )
    assert "approval_comment" not in manifest
    checked_at = datetime.fromisoformat(
        comment["checked_at_utc"].replace("Z", "+00:00")
    )
    assert checked_at == min(source_mtimes)
    assert checked_at <= before <= after
    assert set(comment["checks"]) == {
        "ruleset_bypass_empty",
        "asaas_build_admin_bypass_disabled",
        "asaas_build_branch_only_main",
        "asaas_production_admin_bypass_disabled",
        "asaas_production_branch_only_main",
    }
    for label, filename in packer.SCREENSHOTS.items():
        copied = output / filename
        digest = packer.hashlib.sha256(copied.read_bytes()).hexdigest()
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert manifest["screenshots_sha256"][label] == digest
    assert set(manifest["screenshots_sha256"]) == set(packer.SCREENSHOTS)
    assert set(comment["screenshots"]) == set(packer.SCREENSHOTS)
    assert (
        comment["evidence_sha256"]
        == manifest["screenshots_sha256"][comment["evidence_label"]]
    )
    for label in packer.SCREENSHOTS:
        assert (
            comment["screenshots"][label]["sha256"]
            == manifest["screenshots_sha256"][label]
        )
        assert comment["screenshots"][label]["ref"].startswith(
            "https://github.com/user-attachments/assets/"
        )


def test_packaged_timestamp_is_accepted_by_approval_verifier(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repository)

    comment, _output = packer.package(_args(tmp_path))
    checked_at = comment["checked_at_utc"]

    assert verifier._canonical_timestamp(checked_at).tzinfo == timezone.utc
    assert checked_at.endswith("Z")
    assert len(checked_at.rsplit(".", 1)[1]) == 7


def test_package_and_verifier_share_per_png_size_limit():
    assert packer.MAX_SCREENSHOT_BYTES == verifier.MAX_EVIDENCE_BYTES
    assert packer.MAX_SCREENSHOT_BYTES == 20 * 1024 * 1024


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-16T04:59:34Z",
        "2026-10-16T04:59:34.1Z",
        "2026-10-16T04:59:34.1234567Z",
        "2026-10-16T04:59:34.123456+00:00",
        "2026-10-16T04:59:34.123456z",
        "2026-02-30T04:59:34.123456Z",
    ],
)
def test_approval_verifier_rejects_malformed_or_noncanonical_timestamps(value):
    with pytest.raises(verifier.ApprovalGateError, match="approval_timestamp_invalid"):
        verifier._canonical_timestamp(value)


def test_valid_png_scanline_is_accepted():
    packer._validate_png(_png())


@pytest.mark.parametrize(
    "overrides",
    [
        {"width": 0},
        {"height": 0},
        {"width": packer.MAX_IMAGE_DIMENSION + 1},
        {"bit_depth": 1, "color_type": 6},
        {"bit_depth": 16, "color_type": 3},
        {"color_type": 7},
        {"compression": 1},
        {"filter_method": 1},
        {"interlace": 2},
    ],
)
def test_malformed_ihdr_values_are_rejected(overrides):
    with pytest.raises(ValueError, match="header values are invalid"):
        packer._validate_png(_png(**overrides))


@pytest.mark.parametrize(
    "image_data",
    [
        b"not-zlib",
        zlib.compress(b"\x00\x00\x00\x00\x00")[:-2],
        zlib.compress(b"\x05\x00\x00\x00\x00"),
        zlib.compress(b"\x00\x00\x00"),
    ],
)
def test_crc_valid_but_invalid_or_truncated_idat_is_rejected(image_data):
    with pytest.raises(ValueError, match="image data|scanline"):
        packer._validate_png(_png(image_data=image_data))


@pytest.mark.parametrize("bad_png", [b"not-png", packer.PNG_SIGNATURE])
def test_packer_rejects_invalid_or_empty_png_before_creating_run_directory(
    monkeypatch, tmp_path, bad_png
):
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repository)
    args = _args(tmp_path)
    Path(args.ruleset).write_bytes(bad_png)

    with pytest.raises(ValueError, match="screenshot"):
        packer.package(args)

    assert not (
        home / ".local/share/neurostack/asaas-build-ui-evidence/"
        "neurostackinfra-neurostack-api/run-7123456789-attempt-2"
    ).exists()


def test_packer_rejects_symlink_screenshot(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repository)
    args = _args(tmp_path)
    original = Path(args.ruleset)
    link = original.with_name("ruleset-link.png")
    link.symlink_to(original)
    args.ruleset = str(link)

    with pytest.raises(ValueError, match="non-symlink"):
        packer.package(args)


def test_packer_requires_three_distinct_screenshot_digests(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repository)
    args = _args(tmp_path)
    args.asaas_build = args.ruleset
    args.asaas_production_deployment = args.ruleset

    with pytest.raises(ValueError, match="distinct SHA-256"):
        packer.package(args)

    assert not (
        home / ".local/share/neurostack/asaas-build-ui-evidence/"
        "neurostackinfra-neurostack-api/run-7123456789-attempt-2"
    ).exists()


@pytest.mark.parametrize("age", [timedelta(minutes=-6), timedelta(seconds=61)])
def test_packer_rejects_stale_or_future_screenshot_mtime(monkeypatch, tmp_path, age):
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(repository)
    args = _args(tmp_path)
    source = Path(args.ruleset)
    timestamp = (datetime.now(timezone.utc) + age).timestamp()
    os.utime(source, (timestamp, timestamp))

    with pytest.raises(ValueError, match="mtime is stale or in the future"):
        packer.package(args)


def test_repository_checks_use_script_location_not_current_directory(
    monkeypatch, tmp_path
):
    repository = tmp_path / "actual-repository"
    (repository / "scripts").mkdir(parents=True)
    other_directory = tmp_path / "elsewhere"
    other_directory.mkdir()
    monkeypatch.setattr(packer, "__file__", str(repository / "scripts/packer.py"))
    monkeypatch.chdir(other_directory)
    args = _args(tmp_path)
    in_repo_screenshot = repository / "capture.png"
    in_repo_screenshot.write_bytes(_png())
    args.ruleset = str(in_repo_screenshot)

    with pytest.raises(ValueError, match="outside the repository"):
        packer.package(args)


def test_private_evidence_path_cannot_be_inside_script_repository(
    monkeypatch, tmp_path
):
    repository = tmp_path / "actual-repository"
    (repository / "scripts").mkdir(parents=True)
    monkeypatch.setattr(packer, "__file__", str(repository / "scripts/packer.py"))
    monkeypatch.setenv("HOME", str(repository / "home"))

    with pytest.raises(ValueError, match="outside the repository"):
        packer._private_run_directory("7123456789", "2")
