#!/usr/bin/env python3
"""Fail closed when administrator UI evidence is stale at the OIDC boundary."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

# Keep the UI inspection below the plan's 5-minute maximum at the actual STS
# request; reserve the final 60 seconds for the credentials action handoff.
MAX_UI_EVIDENCE_AGE_SECONDS = 4 * 60


def validate_ui_evidence_freshness(
    checked_at_utc: str, *, now: datetime | None = None
) -> None:
    if not isinstance(checked_at_utc, str) or not checked_at_utc.endswith("Z"):
        raise ValueError("ui_evidence_checked_at_missing_or_invalid")
    try:
        checked_at = datetime.fromisoformat(checked_at_utc[:-1] + "+00:00")
    except ValueError:
        raise ValueError("ui_evidence_checked_at_missing_or_invalid") from None
    current = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    age_seconds = (current - checked_at).total_seconds()
    if age_seconds < 0:
        raise ValueError("ui_evidence_checked_at_in_future")
    if age_seconds >= MAX_UI_EVIDENCE_AGE_SECONDS:
        raise ValueError("ui_evidence_stale_at_oidc_boundary")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checked-at-utc", default="")
    args = parser.parse_args()
    try:
        validate_ui_evidence_freshness(args.checked_at_utc)
    except ValueError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 1
    print("Administrator UI evidence is fresh at the OIDC boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
