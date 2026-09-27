"""
NeuroStack — shared clinic-timezone helpers.

Resolves the clinic's configured IANA timezone into a `ZoneInfo`, with a safe
fallback to `America/Sao_Paulo` on invalid values (and an observable log
warning so misconfigured clinics don't silently serve wrong local time).

Kept as a shared module (not a template-local closure) so future business-hours
/ "fora do horário" logic can reuse the same clinic clock.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

_DEFAULT_TZ = "America/Sao_Paulo"


def resolve_clinic_tz(tz_str: str | None) -> ZoneInfo:
    """Return a `ZoneInfo` for the clinic timezone, falling back safely.

    - `None` / empty → default `America/Sao_Paulo` (normal "unset" case, no warning).
    - Non-empty invalid string → default + `logger.warning` naming the bad value,
      so operators can spot a misconfigured clinic instead of silently serving
      wrong greetings.
    """
    if not tz_str:
        return ZoneInfo(_DEFAULT_TZ)
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        logger.warning(
            "invalid clinic timezone %r; falling back to America/Sao_Paulo",
            tz_str,
        )
        return ZoneInfo(_DEFAULT_TZ)


def clinic_now(tz_str: str | None) -> datetime:
    """Return the current datetime in the clinic's configured timezone."""
    return datetime.now(resolve_clinic_tz(tz_str))
