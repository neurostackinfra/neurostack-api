"""
NeuroStack - Config Service Unit Tests.

Covers the flat<->bucket routing in `unflatten_patch` / `flatten_config`
for fields wired into `FIELD_TO_BUCKET` — specifically the `timezone`
field on the `operations` bucket that drives the clinic-local greeting.

If the `FIELD_TO_BUCKET["timezone"] = "operations"` entry were removed,
`unflatten_patch` would silently drop the field (see
`src/services/config_service.py` — `if bucket is None: continue`) and
these tests would fail.
"""

from types import SimpleNamespace

import pytest

from src.services.config_service import (
    ALL_BUCKETS,
    ARRAY_BUCKETS,
    FIELD_TO_BUCKET,
    flatten_config,
    unflatten_patch,
)


def _empty_config_row(**overrides):
    """Build a lightweight stand-in for a `ClinicConfigTable` row.

    `flatten_config` reads buckets via `getattr(row, bucket, None)`, so a
    `SimpleNamespace` with the expected attributes is enough — no ORM or
    database required.
    """
    row = SimpleNamespace(
        clinic_id=None,
        version=1,
        **{
            bucket: ([] if bucket in ARRAY_BUCKETS else {})
            for bucket in ALL_BUCKETS
        },
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class TestTimezoneConfigRoutingAndRoundTrip:
    """`timezone` must live on the `operations` bucket in both directions."""

    def test_timezone_maps_to_operations_bucket(self):
        assert FIELD_TO_BUCKET["timezone"] == "operations"

    def test_unflatten_patch_routes_timezone_to_operations(self):
        grouped = unflatten_patch({"timezone": "America/New_York"})

        assert "operations" in grouped
        assert grouped["operations"] == {"timezone": "America/New_York"}

    def test_unflatten_patch_does_not_leak_timezone_to_other_buckets(self):
        grouped = unflatten_patch({"timezone": "America/New_York"})

        for bucket, payload in grouped.items():
            if bucket == "operations" or bucket in ARRAY_BUCKETS:
                continue
            assert "timezone" not in payload, (
                f"timezone leaked into non-operations bucket {bucket!r}"
            )

    def test_flatten_config_surfaces_timezone_at_top_level(self):
        row = _empty_config_row(
            operations={"timezone": "America/New_York", "working_hours": "8-18"}
        )

        flat = flatten_config(row)

        assert flat["timezone"] == "America/New_York"
        # Sibling field on the same bucket also promotes — sanity check.
        assert flat["working_hours"] == "8-18"

    def test_round_trip_patch_then_flatten_preserves_timezone(self):
        """Simulate the PATCH -> persist -> GET path end-to-end."""
        grouped = unflatten_patch({"timezone": "America/New_York"})

        row = _empty_config_row()
        for bucket, payload in grouped.items():
            setattr(row, bucket, payload)

        flat = flatten_config(row)

        assert flat["timezone"] == "America/New_York"
