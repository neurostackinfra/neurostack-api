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

from src.pause_policy import parse_legacy_cycle_days

DEFAULT_REACTIVATION_TRIGGER_MESSAGE = "Precisando de algo mais, estamos a disposição."
DEFAULT_REACTIVATION_DELAY_HOURS = 1


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


@dataclass(frozen=True)
class TriggerReactivationDecision:
    """Effective trigger phrase and cooldown for a clinic-authored message."""

    trigger_message: str
    delay_hours: int
    reactivates_at: datetime
    notices: tuple[PausePolicyNotice, ...] = ()


def _enum(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().lower()


def _valid_json_int(value: Any, *, lower: int, upper: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if lower <= value <= upper else None


def resolve_trigger_reactivation(
    handoff: Any,
    now: datetime,
) -> TriggerReactivationDecision:
    """Resolve persisted trigger settings into the exact effective runtime values."""

    bucket = handoff if isinstance(handoff, dict) else {}
    notices: list[PausePolicyNotice] = []

    raw_trigger = bucket.get(
        "trigger_message",
        DEFAULT_REACTIVATION_TRIGGER_MESSAGE,
    )
    if isinstance(raw_trigger, str) and raw_trigger.strip():
        trigger_message = raw_trigger
    else:
        trigger_message = DEFAULT_REACTIVATION_TRIGGER_MESSAGE
        if "trigger_message" in bucket:
            notices.append(PausePolicyNotice("trigger_message", "invalid_phrase"))

    raw_delay = bucket.get(
        "reactivation_delay_hours",
        DEFAULT_REACTIVATION_DELAY_HOURS,
    )
    fallback_reason: str | None = None
    if isinstance(raw_delay, bool) or not isinstance(raw_delay, (int, str)):
        delay_hours = DEFAULT_REACTIVATION_DELAY_HOURS
        fallback_reason = "invalid_duration"
    else:
        try:
            delay_hours = int(raw_delay)
            if delay_hours <= 0:
                delay_hours = DEFAULT_REACTIVATION_DELAY_HOURS
                fallback_reason = "invalid_duration"
        except ValueError:
            delay_hours = DEFAULT_REACTIVATION_DELAY_HOURS
            fallback_reason = "invalid_duration"

    try:
        reactivates_at = now + timedelta(hours=delay_hours)
    except OverflowError:
        delay_hours = DEFAULT_REACTIVATION_DELAY_HOURS
        reactivates_at = now + timedelta(hours=delay_hours)
        fallback_reason = "unrepresentable_duration"

    if fallback_reason is not None:
        notices.append(PausePolicyNotice("reactivation_delay_hours", fallback_reason))

    return TriggerReactivationDecision(
        trigger_message=trigger_message,
        delay_hours=delay_hours,
        reactivates_at=reactivates_at,
        notices=tuple(notices),
    )


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
