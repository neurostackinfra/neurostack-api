"""backfill weekly business_hours from legacy single-window config

Revision ID: 017
Revises: 016
Create Date: 2026-09-02 00:00:01

Idempotently upgrades ``clinic_configs.operations.business_hours`` from the
legacy shape (four flat fields: ``business_hours_enabled``,
``business_hours_open``, ``business_hours_close``, ``business_hours_days``)
to the weekly map used by the new classifier and formatter:

    business_hours = {"mon": {"open": "09:00", "close": "18:00"}, ...}

This migration is additive-only.  It never deletes the legacy keys — the
frontend still reads them from the flat config until Group 2 ships, and
leaving them behind keeps the write path fully reversible even after this
migration runs.  If ``operations.business_hours`` already exists on a row
the mapping is a no-op (idempotent), so re-running this migration is safe.

Rows whose legacy configuration is disabled, incomplete, or malformed
(missing / invalid ``open`` or ``close``, ``close <= open``, empty or
invalid ``days``) are left with no ``business_hours`` field at all — the
downstream classifier already treats "missing schedule" as "unconfigured".
"""

from __future__ import annotations

import re
from typing import Union

from alembic import op
from sqlalchemy.sql import bindparam, column, table

revision: str = "017"
down_revision: Union[str, None] = "016"
branch_labels = None
depends_on = None

_VALID_DAY_CODES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_HHMM_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _hhmm_to_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def upgrade_legacy_business_hours(operations: dict) -> dict | None:
    """Return the new ``business_hours`` value for a legacy operations dict.

    Frozen mapping logic — exported so tests can pin it and so future readers
    can audit exactly how legacy data was reshaped.  Rules:

    * Existing ``business_hours`` wins → return it unchanged (idempotent).
    * ``business_hours_enabled`` must be exactly ``True``.
    * ``business_hours_open`` and ``business_hours_close`` must match
      ``HH:MM`` and satisfy ``close > open`` (minutes-of-day).
    * ``business_hours_days`` must be a non-empty list of valid codes.
    * Any other combination → ``None`` (write nothing).
    """
    if not isinstance(operations, dict):
        return None

    existing = operations.get("business_hours")
    if isinstance(existing, dict) and existing:
        return existing

    if operations.get("business_hours_enabled") is not True:
        return None

    open_str = operations.get("business_hours_open")
    close_str = operations.get("business_hours_close")
    if not isinstance(open_str, str) or _HHMM_PATTERN.fullmatch(open_str) is None:
        return None
    if not isinstance(close_str, str) or _HHMM_PATTERN.fullmatch(close_str) is None:
        return None
    if _hhmm_to_minutes(close_str) <= _hhmm_to_minutes(open_str):
        return None

    days = operations.get("business_hours_days")
    if not isinstance(days, list) or not days:
        return None
    normalized_days: list[str] = []
    for day in days:
        if not isinstance(day, str):
            return None
        code = day.lower()
        if code not in _VALID_DAY_CODES:
            return None
        normalized_days.append(code)

    schedule = {day: {"open": open_str, "close": close_str} for day in normalized_days}
    return schedule


def upgrade() -> None:
    connection = op.get_bind()
    clinic_configs = table(
        "clinic_configs",
        column("id"),
        column("operations"),
    )
    rows = connection.execute(
        clinic_configs.select().with_only_columns(
            clinic_configs.c.id, clinic_configs.c.operations
        )
    ).fetchall()

    for row in rows:
        operations = dict(row.operations or {})
        schedule = upgrade_legacy_business_hours(operations)
        if schedule is None:
            continue
        # Idempotency: if the value is already present and equal, skip write.
        if operations.get("business_hours") == schedule:
            continue
        new_operations = dict(operations)
        new_operations["business_hours"] = schedule
        connection.execute(
            clinic_configs.update()
            .where(clinic_configs.c.id == bindparam("row_id"))
            .values(operations=bindparam("new_ops")),
            [{"row_id": row.id, "new_ops": new_operations}],
        )


def downgrade() -> None:
    """No-op by design.

    Reversing this migration would be lossy for any multi-window schedule
    created after the upgrade (the legacy shape can only represent one
    open/close window shared across a flat list of days).  Because callers
    treat "missing schedule" as "not configured", the safest downgrade is
    to leave the ``business_hours`` map in place: a wrong business-hours
    value is worse than an absent one.
    """
    pass
