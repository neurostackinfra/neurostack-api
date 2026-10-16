"""Pure parsers shared by clinic configuration and handoff policy code."""

from __future__ import annotations

from typing import Any


def _valid_json_int(value: Any, *, lower: int, upper: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if lower <= value <= upper else None


def parse_legacy_cycle_days(value: Any) -> int | None:
    """Parse bounded canonical/legacy day values without unbounded ``int``."""

    numeric = _valid_json_int(value, lower=1, upper=365)
    if numeric is not None:
        return numeric
    if isinstance(value, str) and value and value.isascii() and value.isdigit():
        normalized = value.lstrip("0") or "0"
        if len(normalized) > 3:
            return None
        parsed = int(normalized)
        return parsed if 1 <= parsed <= 365 else None
    return None
