"""Pure resolution of the post-handoff pause policy.

Persisted JSON predates the strict configuration boundary, so this module is
deliberately tolerant.  It never raises for an arbitrary JSON shape and never
logs by itself; callers may emit the returned structured notices with their
own tenant/session context.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class PauseAction(str, Enum):
    """The three possible runtime outcomes."""

    NO_PAUSE = "no_pause"
    PAUSE_FOREVER = "pause_forever"
    PAUSE_UNTIL = "pause_until"


@dataclass(frozen=True)
class PausePolicyNotice:
    """A safe-to-log description of tolerated historical configuration."""

    field: str
    reason: str


@dataclass(frozen=True)
class PauseDecision:
    """Resolved pause plus non-sensitive compatibility notices."""

    action: PauseAction
    paused_until: datetime | None = None
    notices: tuple[PausePolicyNotice, ...] = ()

    @property
    def should_pause(self) -> bool:
        return self.action is not PauseAction.NO_PAUSE


def _enum(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().lower()


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


def resolve_handoff_pause(handoff: Any, now: datetime) -> PauseDecision:
    """Resolve an arbitrary persisted handoff bucket without performing I/O."""

    bucket = handoff if isinstance(handoff, dict) else {}

    active_raw = bucket.get("handoff_active", "yes")
    if _enum(active_raw) != "yes":
        return PauseDecision(PauseAction.NO_PAUSE)

    notices: list[PausePolicyNotice] = []
    standby_raw = bucket.get("standby_mode", "active")
    standby_mode = _enum(standby_raw)
    if standby_mode not in {"inactive", "active"}:
        notices.append(PausePolicyNotice("standby_mode", "invalid_enum"))
        return PauseDecision(
            PauseAction.PAUSE_FOREVER,
            notices=tuple(notices),
        )

    if standby_mode == "inactive":
        return PauseDecision(
            PauseAction.PAUSE_UNTIL,
            paused_until=now + timedelta(hours=24),
        )

    period_present = "handoff_period" in bucket
    period = _enum(bucket.get("handoff_period", "permanent"))
    if period not in {"permanent", "cycle"}:
        if period_present:
            notices.append(PausePolicyNotice("handoff_period", "invalid_enum"))
        return PauseDecision(
            PauseAction.PAUSE_FOREVER,
            notices=tuple(notices),
        )

    if period == "permanent":
        return PauseDecision(PauseAction.PAUSE_FOREVER)

    hours_raw = bucket.get("handoff_cycle_hours")
    days_raw = bucket.get("handoff_cycle_days")
    hours = _valid_json_int(hours_raw, lower=1, upper=720)
    days = parse_legacy_cycle_days(days_raw)
    hours_filled = hours_raw is not None
    days_filled = days_raw is not None

    if hours_filled and days_filled:
        notices.append(PausePolicyNotice("handoff_cycle", "multiple_durations"))

    if hours is not None:
        return PauseDecision(
            PauseAction.PAUSE_UNTIL,
            paused_until=now + timedelta(hours=hours),
            notices=tuple(notices),
        )
    if days is not None:
        return PauseDecision(
            PauseAction.PAUSE_UNTIL,
            paused_until=now + timedelta(days=days),
            notices=tuple(notices),
        )

    if hours_filled:
        notices.append(PausePolicyNotice("handoff_cycle_hours", "invalid_duration"))
    if days_filled:
        notices.append(PausePolicyNotice("handoff_cycle_days", "invalid_duration"))
    notices.append(PausePolicyNotice("handoff_cycle", "fallback_seven_days"))
    return PauseDecision(
        PauseAction.PAUSE_UNTIL,
        paused_until=now + timedelta(days=7),
        notices=tuple(notices),
    )
