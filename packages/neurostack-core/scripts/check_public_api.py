#!/usr/bin/env python3
"""Verify the exact public facades declared by a captured JSON fixture."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import ModuleType
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FACADE_NAMES = (
    "neurostack_core.flow",
    "neurostack_core.policy",
    "neurostack_core.prompt",
    "neurostack_core.srm",
)


def _load_facades() -> dict[str, ModuleType]:
    import neurostack_core.flow as flow_facade
    import neurostack_core.policy as policy_facade
    import neurostack_core.prompt as prompt_facade
    import neurostack_core.srm as srm_facade

    return {
        "neurostack_core.flow": flow_facade,
        "neurostack_core.policy": policy_facade,
        "neurostack_core.prompt": prompt_facade,
        "neurostack_core.srm": srm_facade,
    }


def _load_expected(path: Path) -> dict[str, list[str]]:
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("public API fixture must be a JSON object")

    expected: dict[str, list[str]] = {}
    for module_name, symbols in data.items():
        if not isinstance(module_name, str) or not isinstance(symbols, list):
            raise ValueError("public API fixture must map module names to symbol lists")
        if not all(isinstance(symbol, str) for symbol in symbols):
            raise ValueError(f"{module_name} contains a non-string symbol")
        if len(symbols) != len(set(symbols)):
            raise ValueError(f"{module_name} contains duplicate symbols")
        expected[module_name] = symbols
    if set(expected) != set(FACADE_NAMES):
        raise ValueError(
            f"public API fixture modules must be exactly {sorted(FACADE_NAMES)}"
        )
    return expected


def _inspect_facades(
    expected: dict[str, list[str]],
    required_origin: Path | None,
    facades: dict[str, ModuleType],
) -> dict[str, list[str]]:
    actual: dict[str, list[str]] = {}
    for module_name in expected:
        module = facades[module_name]
        module_file = getattr(module, "__file__", None)
        if required_origin is not None:
            if module_file is None:
                raise RuntimeError(f"{module_name} has no import origin")
            expected_file = required_origin.joinpath(
                *module_name.split(".")[1:], "__init__.py"
            ).resolve()
            if Path(module_file).resolve() != expected_file:
                raise RuntimeError(
                    f"{module_name} imported from {module_file}, "
                    f"expected exact origin {expected_file}"
                )

        exports = getattr(module, "__all__", None)
        if not isinstance(exports, list) or not all(
            isinstance(symbol, str) for symbol in exports
        ):
            raise RuntimeError(
                f"{module_name} must define __all__ as a list of strings"
            )
        for symbol in expected[module_name]:
            if not hasattr(module, symbol):
                raise RuntimeError(
                    f"{module_name} does not define expected symbol {symbol}"
                )
        actual[module_name] = exports
    return actual


def _safe_record_path(site_packages: Path, raw_name: str) -> Path:
    """Resolve one canonical POSIX RECORD member below site-packages."""
    if (
        not raw_name
        or "\\" in raw_name
        or raw_name.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_name.split("/"))
    ):
        raise RuntimeError(f"unsafe RECORD member {raw_name!r}")
    member = (site_packages / Path(*raw_name.split("/"))).resolve()
    try:
        member.relative_to(site_packages)
    except ValueError as exc:
        raise RuntimeError(
            f"RECORD member escapes site-packages: {raw_name!r}"
        ) from exc
    return member


def _verify_record(dist_info: Path, package_root: Path) -> None:
    """Verify complete, safe, content-addressed installed distribution records."""
    site_packages = dist_info.parent.resolve()
    record = dist_info / "RECORD"
    if not record.is_file():
        raise RuntimeError("installed neurostack-core distribution has no RECORD")

    rows: dict[str, tuple[str, str, Path]] = {}
    with record.open(encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle):
            if len(row) != 3:
                raise RuntimeError(f"malformed RECORD row: {row!r}")
            raw_name, encoded_hash, raw_size = row
            if raw_name in rows:
                raise RuntimeError(f"duplicate RECORD member {raw_name!r}")
            rows[raw_name] = (
                encoded_hash,
                raw_size,
                _safe_record_path(site_packages, raw_name),
            )

    for raw_name, (_, _, member) in rows.items():
        if not (
            member.is_relative_to(package_root) or member.is_relative_to(dist_info)
        ):
            raise RuntimeError(f"RECORD contains foreign member: {raw_name}")

    record_name = record.relative_to(site_packages).as_posix()
    for raw_name, (encoded_hash, raw_size, member) in rows.items():
        if not member.is_file():
            raise RuntimeError(f"RECORD member is missing: {raw_name}")
        if raw_name == record_name:
            if encoded_hash or raw_size:
                raise RuntimeError("RECORD self-row must omit hash and size")
            continue
        generated_bytecode = (
            "__pycache__" in member.parts
            and member.suffix == ".pyc"
            and member.is_relative_to(package_root)
        )
        if generated_bytecode and not encoded_hash and not raw_size:
            continue
        if not encoded_hash.startswith("sha256=") or not raw_size.isdecimal():
            raise RuntimeError(f"RECORD member lacks sha256 hash/size: {raw_name}")
        content = member.read_bytes()
        actual_hash = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        if encoded_hash != f"sha256={actual_hash}":
            raise RuntimeError(f"RECORD hash mismatch: {raw_name}")
        if int(raw_size) != len(content):
            raise RuntimeError(f"RECORD size mismatch: {raw_name}")

    recorded_files = {
        entry[2] for entry in rows.values() if "__pycache__" not in entry[2].parts
    }
    owned_files = {
        path.resolve()
        for root in (package_root, dist_info)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    if recorded_files != owned_files:
        missing = sorted(str(path) for path in owned_files - recorded_files)
        extra = sorted(str(path) for path in recorded_files - owned_files)
        raise RuntimeError(
            f"installed files and RECORD differ; missing={missing}, extra={extra}"
        )


def _installed_origin() -> Path:
    """Return the exact package root owned by one canonical distribution."""
    pythonpath = os.environ.get("PYTHONPATH")
    if pythonpath:
        raise RuntimeError("installed check refuses ambient PYTHONPATH")

    cwd_shadow = Path.cwd() / "neurostack_core"
    if cwd_shadow.exists():
        raise RuntimeError(f"installed check refuses cwd shadow {cwd_shadow}")

    prefix = Path(sys.prefix).resolve()
    metadata_files: list[Path] = []
    for entry in sys.path:
        if not entry:
            continue
        search_root = Path(entry).resolve()
        try:
            search_root.relative_to(prefix)
        except ValueError:
            continue
        metadata_files.extend(search_root.glob("neurostack_core-*.dist-info/METADATA"))

    matching_metadata: set[Path] = set()
    for metadata_file in metadata_files:
        metadata = BytesParser(policy=policy.default).parsebytes(
            metadata_file.read_bytes()
        )
        name = str(metadata.get("Name", "")).casefold().replace("_", "-")
        if name == "neurostack-core":
            matching_metadata.add(metadata_file.resolve())
    if len(matching_metadata) != 1:
        raise RuntimeError(
            "expected one neurostack-core distribution in the active environment, "
            f"found {len(matching_metadata)}"
        )

    metadata_file = next(iter(matching_metadata))
    dist_info = metadata_file.parent
    metadata = BytesParser(policy=policy.default).parsebytes(metadata_file.read_bytes())
    version = str(metadata.get("Version", ""))
    normalized_version = version.replace("-", "_")
    if dist_info.name != f"neurostack_core-{normalized_version}.dist-info":
        raise RuntimeError(f"non-canonical dist-info directory {dist_info.name!r}")

    package_root = (dist_info.parent / "neurostack_core").resolve()
    if package_root.parent != dist_info.parent or not package_root.is_dir():
        raise RuntimeError("installed distribution has no canonical package root")
    _verify_record(dist_info, package_root)
    return package_root


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True)
    origins = parser.add_mutually_exclusive_group()
    origins.add_argument(
        "--source",
        action="store_true",
        help="import from and verify the adjacent neurostack-core source tree",
    )
    origins.add_argument(
        "--installed",
        action="store_true",
        help="require a non-editable import from the active environment",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        expected = _load_expected(args.expected)
        required_origin: Path | None = None
        if args.source:
            required_origin = PACKAGE_ROOT / "neurostack_core"
            sys.path.insert(0, str(PACKAGE_ROOT))
        elif args.installed:
            required_origin = _installed_origin()

        actual = _inspect_facades(expected, required_origin, _load_facades())
        if actual != expected:
            raise RuntimeError(
                "public facade mismatch\n"
                f"expected: {json.dumps(expected, sort_keys=True)}\n"
                f"actual:   {json.dumps(actual, sort_keys=True)}"
            )
    except (
        ImportError,
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"public API check failed: {exc}", file=sys.stderr)
        return 1

    print(f"public API matches {args.expected}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
