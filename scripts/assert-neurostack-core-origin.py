#!/usr/bin/env python3
"""Prove that neurostack-core was installed from the intended surface."""

from __future__ import annotations

import argparse
import json
import os
import re
import site
import sys
from importlib import import_module, metadata
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo


class OriginError(ValueError):
    pass


def within(path: Path, prefix: Path) -> bool:
    try:
        path.resolve().relative_to(prefix.resolve())
        return True
    except ValueError:
        return False


def direct_url(dist: metadata.Distribution) -> tuple[Path | None, bool]:
    raw = dist.read_text("direct_url.json")
    if raw is None:
        return None, False
    payload = json.loads(raw)
    url = payload.get("url")
    if not isinstance(url, str):
        raise OriginError("direct_url.json has no URL")
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise OriginError(f"direct URL is not local: {url}")
    directory = payload.get("dir_info")
    editable = isinstance(directory, dict) and directory.get("editable") is True
    return Path(unquote(parsed.path)), editable


def active_prefixes() -> tuple[Path, ...]:
    values = {Path(sys.prefix).resolve()}
    for value in site.getsitepackages():
        values.add(Path(value).resolve())
    return tuple(values)


def requirement_name(raw: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", raw)
    if match is None:
        raise OriginError(f"invalid Requires-Dist entry: {raw}")
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def validate(mode: str, expected_source: Path | None) -> None:
    package = import_module("neurostack_core")
    package_file = getattr(package, "__file__", None)
    if not isinstance(package_file, str):
        raise OriginError("neurostack_core has no import origin")
    origin = Path(package_file).resolve()
    dist = metadata.distribution("neurostack-core")
    dist_path = Path(getattr(dist, "_path", dist.locate_file(""))).resolve()
    prefixes = active_prefixes()
    if not any(within(dist_path, prefix) for prefix in prefixes):
        raise OriginError(f"dist-info is outside the active environment: {dist_path}")
    env_pythonpath = [
        Path(entry).resolve()
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if entry
    ]
    if any(within(origin, entry) for entry in env_pythonpath):
        raise OriginError(f"import origin leaked through PYTHONPATH: {origin}")
    cwd = Path.cwd().resolve()
    expected_contains_origin = expected_source is not None and within(
        origin, expected_source.resolve()
    )
    environment_contains_origin = any(within(origin, prefix) for prefix in prefixes)
    if (
        within(origin, cwd)
        and not expected_contains_origin
        and not environment_contains_origin
    ):
        raise OriginError(f"import origin leaked through cwd: {origin}")
    shadow = cwd / "neurostack_core"
    if shadow.exists() and not (
        expected_source is not None and within(shadow, expected_source.resolve())
    ):
        raise OriginError(f"cwd contains a shadow neurostack_core package: {shadow}")
    direct, editable = direct_url(dist)
    if mode == "editable":
        if expected_source is None:
            raise OriginError("editable validation requires --expected-source")
        source = expected_source.resolve()
        if not within(origin, source):
            raise OriginError(f"editable code is not under {source}: {origin}")
        if direct is None or direct.resolve() != source:
            raise OriginError(
                f"editable direct_url does not point to {source}: {direct}"
            )
        if not editable:
            raise OriginError("direct_url.json does not mark the install editable")
    else:
        if not any(within(origin, prefix) for prefix in prefixes):
            raise OriginError(
                f"noneditable code is outside the active environment: {origin}"
            )
        if expected_source is not None and within(origin, expected_source.resolve()):
            raise OriginError("noneditable import resolved to the source tree")
    requirements = {requirement_name(raw) for raw in (dist.requires or ())}
    required = {"pydantic", "prometheus-client", "tzdata"}
    missing = required - requirements
    if missing:
        raise OriginError(
            "neurostack-core metadata is missing requirements: "
            + ", ".join(sorted(missing))
        )
    tzdata = metadata.distribution("tzdata")
    if not tzdata.version:
        raise OriginError("tzdata distribution has no version")
    if os.environ.get("PYTHONTZPATH") != "":
        raise OriginError('PYTHONTZPATH must be set to "" for the timezone proof')
    zone = ZoneInfo("America/Sao_Paulo")
    if zone.key != "America/Sao_Paulo":
        raise OriginError("tzdata ZoneInfo lookup returned the wrong key")
    print(f"neurostack-core origin valid ({mode}): {origin}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("editable", "noneditable"), required=True)
    parser.add_argument("--expected-source", type=Path)
    args = parser.parse_args()
    try:
        validate(args.mode, args.expected_source)
    except (OriginError, metadata.PackageNotFoundError, json.JSONDecodeError) as exc:
        print(f"origin validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
