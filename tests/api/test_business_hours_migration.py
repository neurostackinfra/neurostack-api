"""Tests for the frozen mapping in migration 017 (business hours schedule).

The mapping logic lives in the migration only; we load it via
``importlib.util.spec_from_file_location`` so the migration path stays the
single source of truth for how legacy business-hours config was reshaped.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "20260902_000001_017_business_hours_weekly_schedule.py"
)


def _load_migration() -> ModuleType:
    # The repository ships its own ``alembic/`` directory (the migrations
    # package alembic itself uses at runtime).  When pytest adds the repo
    # root to ``sys.path``, that local package shadows the installed
    # ``alembic`` distribution, so ``from alembic import op`` — the very
    # first import inside migration 017 — fails with ``ImportError``.
    # Alembic's own runner sidesteps this by executing migrations under
    # its own module resolver; for a standalone importlib load we stub
    # the ``op`` symbol on the shadowing package.  We only exercise
    # ``upgrade_legacy_business_hours``, which is pure, so a placeholder
    # is enough — the real ``op`` is never called from these tests.
    # We force-import the shadowing ``alembic`` package here so the stub
    # is guaranteed to be attached before the migration module executes
    # its own ``from alembic import op``.
    import alembic as _alembic_pkg

    if not hasattr(_alembic_pkg, "op"):
        _alembic_pkg.op = SimpleNamespace()  # type: ignore[attr-defined]

    spec = importlib.util.spec_from_file_location(
        "migration_017_business_hours", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load_migration()


def test_legacy_enabled_config_maps_to_per_day_schedule(migration: ModuleType) -> None:
    operations = {
        "business_hours_enabled": True,
        "business_hours_open": "09:00",
        "business_hours_close": "18:00",
        "business_hours_days": ["mon", "tue", "wed", "thu", "fri"],
    }

    schedule = migration.upgrade_legacy_business_hours(operations)

    assert schedule == {
        "mon": {"open": "09:00", "close": "18:00"},
        "tue": {"open": "09:00", "close": "18:00"},
        "wed": {"open": "09:00", "close": "18:00"},
        "thu": {"open": "09:00", "close": "18:00"},
        "fri": {"open": "09:00", "close": "18:00"},
    }


@pytest.mark.parametrize(
    "operations",
    [
        # Disabled → nothing written
        {
            "business_hours_enabled": False,
            "business_hours_open": "09:00",
            "business_hours_close": "18:00",
            "business_hours_days": ["mon"],
        },
        # Missing enabled flag entirely
        {
            "business_hours_open": "09:00",
            "business_hours_close": "18:00",
            "business_hours_days": ["mon"],
        },
        # Empty days list
        {
            "business_hours_enabled": True,
            "business_hours_open": "09:00",
            "business_hours_close": "18:00",
            "business_hours_days": [],
        },
        # close <= open
        {
            "business_hours_enabled": True,
            "business_hours_open": "18:00",
            "business_hours_close": "09:00",
            "business_hours_days": ["mon"],
        },
        # close == open
        {
            "business_hours_enabled": True,
            "business_hours_open": "09:00",
            "business_hours_close": "09:00",
            "business_hours_days": ["mon"],
        },
        # Malformed open time
        {
            "business_hours_enabled": True,
            "business_hours_open": "9h",
            "business_hours_close": "18:00",
            "business_hours_days": ["mon"],
        },
        # Malformed close time
        {
            "business_hours_enabled": True,
            "business_hours_open": "09:00",
            "business_hours_close": "25:00",
            "business_hours_days": ["mon"],
        },
        # Invalid day code
        {
            "business_hours_enabled": True,
            "business_hours_open": "09:00",
            "business_hours_close": "18:00",
            "business_hours_days": ["funday"],
        },
        # Not a dict at all
        None,
        "not a dict",
    ],
    ids=[
        "disabled",
        "missing-enabled",
        "empty-days",
        "close-before-open",
        "close-equals-open",
        "bad-open",
        "bad-close",
        "invalid-day",
        "none",
        "non-dict",
    ],
)
def test_incomplete_or_disabled_configs_write_nothing(
    migration: ModuleType, operations
) -> None:
    assert migration.upgrade_legacy_business_hours(operations) is None


def test_row_with_existing_business_hours_is_skipped(migration: ModuleType) -> None:
    existing = {
        "business_hours_enabled": True,
        "business_hours_open": "09:00",
        "business_hours_close": "18:00",
        "business_hours_days": ["mon"],
        "business_hours": {"sat": {"open": "10:00", "close": "13:00"}},
    }

    result = migration.upgrade_legacy_business_hours(existing)

    # Idempotency: return the existing map unchanged so the migration loop
    # sees "already present" and skips the write.
    assert result == {"sat": {"open": "10:00", "close": "13:00"}}


def test_legacy_keys_survive_the_upgrade_mapping(migration: ModuleType) -> None:
    """The mapping is additive — it computes the new value but never mutates
    the input's legacy keys.  Migration is additive-only."""
    operations = {
        "business_hours_enabled": True,
        "business_hours_open": "09:00",
        "business_hours_close": "18:00",
        "business_hours_days": ["mon", "fri"],
    }
    before = dict(operations)

    migration.upgrade_legacy_business_hours(operations)

    assert operations == before
    # Explicit assertion the four legacy keys remain intact.
    for key in (
        "business_hours_enabled",
        "business_hours_open",
        "business_hours_close",
        "business_hours_days",
    ):
        assert key in operations
