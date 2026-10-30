"""RAG and media reservation rules (neurostack-credit-system Group 4a).

Pure: the conversions of the PDF, rounded ONCE on the aggregate (never per
file or chunk), the high-water coverage of a window (no refund), the RAG
reprocessing included up to 100 %, the egress allowance of 200 MB per media
credit with the overage per 100 MB, and the accounting window of owner
decision C (civil dates of ``America/Sao_Paulo``).  Property tests pin the
boundaries.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.services.credit_allocations import (
    EGRESS_INCLUDED_BYTES_PER_CREDIT,
    EGRESS_OVERAGE_BYTES_PER_CREDIT,
    ENFORCED_MEDIA_MAX_BYTES,
    KNOWLEDGE_BATCH_MAX_FILES,
    KNOWLEDGE_DOCUMENT_MAX_CHARACTERS,
    KNOWLEDGE_FILE_MAX_BYTES,
    MEDIA_BYTES_PER_CREDIT,
    RAG_CHARACTERS_PER_CREDIT,
    AccountingWindow,
    accounting_window,
    aggregate_target_mc,
    charge_delta,
    egress_allowance_bytes,
    egress_effect_id,
    egress_target_mc,
    maintenance_effect_id,
    opening_source_key,
    rag_units,
    reprocessing_effect_id,
    reservation_effect_id,
)
from src.services.credit_ledger import CREDIT_TIMEZONE, start_of_civil_day


def _at(day: date, hour: int = 12) -> datetime:
    return start_of_civil_day(day) + timedelta(hours=hour)


# ---------------------------------------------------------------------------
# The PDF's units and limits.
# ---------------------------------------------------------------------------


def test_the_units_and_limits_are_the_pdfs() -> None:
    assert RAG_CHARACTERS_PER_CREDIT == 5_000
    assert MEDIA_BYTES_PER_CREDIT == 20_000_000
    assert EGRESS_INCLUDED_BYTES_PER_CREDIT == 200_000_000
    assert EGRESS_OVERAGE_BYTES_PER_CREDIT == 100_000_000
    assert KNOWLEDGE_DOCUMENT_MAX_CHARACTERS == 500_000
    assert KNOWLEDGE_FILE_MAX_BYTES == 20_000_000
    assert KNOWLEDGE_BATCH_MAX_FILES == 10
    # Only the five types media_assets stores (M4A/OGG-Opus deferred).
    assert ENFORCED_MEDIA_MAX_BYTES == {
        "image": 5_000_000,
        "pdf": 20_000_000,
        "audio": 16_000_000,
        "video": 16_000_000,
    }


@pytest.mark.parametrize(
    ("category", "units", "target"),
    [
        ("rag", 0, 0),
        ("rag", 1, 1),
        ("rag", 4_999, 1_000),
        ("rag", 5_000, 1_000),
        ("rag", 5_001, 1_001),
        ("rag", 500_000, 100_000),
        ("media", 1, 1),
        ("media", 20_000, 1),
        ("media", 20_001, 2),
        ("media", 20_000_000, 1_000),
        ("media", 20_000_001, 1_001),
        ("egress", 100_000, 1),
        ("egress", 100_000_000, 1_000),
        ("egress", 100_000_001, 1_001),
    ],
)
def test_the_aggregate_target_is_rounded_up_in_millicredits(
    category: str, units: int, target: int
) -> None:
    assert aggregate_target_mc(units, category) == target  # type: ignore[arg-type]


@settings(max_examples=300, deadline=None)
@given(
    category=st.sampled_from(["rag", "media", "egress"]),
    sizes=st.lists(st.integers(min_value=0, max_value=60_000_000), max_size=40),
)
def test_rounding_happens_once_on_the_aggregate_never_per_file(
    category: str, sizes: list[int]
) -> None:
    total = aggregate_target_mc(sum(sizes), category)  # type: ignore[arg-type]
    per_file = sum(aggregate_target_mc(size, category) for size in sizes)  # type: ignore[arg-type]
    # One rounding: never more than per-file rounding, never below the exact
    # value, and less than one millicredit above it.
    unit = {"rag": 5_000, "media": 20_000_000, "egress": 100_000_000}[category]
    exact_numerator = sum(sizes) * 1_000
    assert total * unit >= exact_numerator > (total - 1) * unit or total == 0
    assert total <= per_file
    if len([size for size in sizes if size % unit]) > 1:
        # Two fragments with a remainder each: per-file rounding overcharges.
        assert per_file >= total


@settings(max_examples=300, deadline=None)
@given(targets=st.lists(st.integers(min_value=0, max_value=10_000_000), max_size=30))
def test_the_coverage_is_a_high_water_mark_and_charges_sum_to_it(
    targets: list[int],
) -> None:
    """Each delta charges what the target adds over the coverage; a lower
    target (deleted content) charges nothing and refunds nothing; the
    charges of a window always add up to its largest target."""

    coverage = 0
    charged = 0
    for target in targets:
        delta = charge_delta(target, coverage)
        assert delta >= 0
        charged += delta
        coverage = max(coverage, target)
        assert charged == coverage
    assert charged == max(targets, default=0)


def test_deleted_content_frees_coverage_that_new_content_reuses() -> None:
    coverage = 0
    first = aggregate_target_mc(10_000, "rag")  # 2 credits
    assert charge_delta(first, coverage) == 2_000
    coverage = first
    # The document is deleted: the target drops to 0, nothing is refunded.
    assert charge_delta(aggregate_target_mc(0, "rag"), coverage) == 0
    # A new document of the same size in the same window reuses the coverage.
    assert charge_delta(aggregate_target_mc(10_000, "rag"), coverage) == 0
    # A larger one pays only the delta.
    assert charge_delta(aggregate_target_mc(12_000, "rag"), coverage) == 400


# ---------------------------------------------------------------------------
# RAG reprocessing and media egress.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("active", "reprocessed", "units"),
    [
        (10_000, 0, 10_000),
        (10_000, 10_000, 10_000),  # 100 % included
        (10_000, 10_001, 10_001),
        (10_000, 15_001, 15_001),
        (0, 7, 7),
    ],
)
def test_reprocessing_is_included_up_to_the_active_characters(
    active: int, reprocessed: int, units: int
) -> None:
    assert rag_units(active, reprocessed) == units


@settings(max_examples=300, deadline=None)
@given(
    active=st.integers(min_value=0, max_value=2_000_000),
    jobs=st.lists(st.integers(min_value=0, max_value=1_000_000), max_size=20),
)
def test_excess_reprocessing_is_charged_through_the_aggregate_delta(
    active: int, jobs: list[int]
) -> None:
    coverage = aggregate_target_mc(active, "rag")
    charged = 0
    reprocessed = 0
    for job in jobs:
        reprocessed += job
        target = aggregate_target_mc(rag_units(active, reprocessed), "rag")
        charged += charge_delta(target, coverage)
        coverage = max(coverage, target)
    excess = max(reprocessed - active, 0)
    assert coverage + 0 == aggregate_target_mc(active + excess, "rag")
    assert charged == coverage - aggregate_target_mc(active, "rag")


def test_egress_allowance_and_overage() -> None:
    # One media credit covered includes 200 MB of downloads.
    assert egress_allowance_bytes(1_000) == 200_000_000
    assert egress_allowance_bytes(1) == 200_000
    assert egress_target_mc(200_000_000, 200_000_000) == 0
    assert egress_target_mc(200_000_001, 200_000_000) == 1
    assert egress_target_mc(300_000_000, 200_000_000) == 1_000
    # The overage never adds allowance: it is a separate category.
    assert egress_target_mc(10, 0) == 1


@settings(max_examples=300, deadline=None)
@given(
    media_coverage=st.integers(min_value=0, max_value=50_000),
    deliveries=st.lists(st.integers(min_value=1, max_value=60_000_000), max_size=40),
)
def test_egress_is_rounded_once_per_window_and_every_delivery_counts(
    media_coverage: int, deliveries: list[int]
) -> None:
    allowance = egress_allowance_bytes(media_coverage)
    coverage = 0
    charged = 0
    delivered = 0
    for size in deliveries:
        delivered += size
        target = egress_target_mc(delivered, allowance)
        charged += charge_delta(target, coverage)
        coverage = max(coverage, target)
    assert charged == egress_target_mc(sum(deliveries), allowance)


@pytest.mark.parametrize(
    "call",
    [
        lambda: aggregate_target_mc(-1, "rag"),
        lambda: charge_delta(-1, 0),
        lambda: rag_units(1, -1),
        lambda: egress_allowance_bytes(-1),
        lambda: egress_target_mc(-1, 0),
        lambda: aggregate_target_mc(True, "rag"),  # type: ignore[arg-type]
    ],
)
def test_negative_or_non_integer_units_are_refused(call: object) -> None:
    with pytest.raises(ValueError):
        call()  # type: ignore[operator]


# ---------------------------------------------------------------------------
# The accounting window (owner decision C).
# ---------------------------------------------------------------------------


def test_a_subscription_cycle_is_its_own_window() -> None:
    cycle = (date(2026, 10, 15), date(2026, 11, 15))
    window = accounting_window([cycle], _at(date(2026, 10, 20)))
    assert window == AccountingWindow(*cycle, "cycle")
    assert window.starts_at == start_of_civil_day(date(2026, 10, 15))


def test_the_window_starts_at_the_civil_start_of_cycle_start() -> None:
    cycle = (date(2026, 10, 15), date(2026, 11, 15))
    start = start_of_civil_day(date(2026, 10, 15))
    assert accounting_window([cycle], start).kind == "cycle"
    before = accounting_window([cycle], start - timedelta(microseconds=1))
    # Before its cycle_start an early renewal never decides the window.
    assert before.kind == "calendar" and before.start == date(2026, 10, 1)


def test_an_early_renewal_does_not_open_its_window_before_cycle_start() -> None:
    current = (date(2026, 10, 15), date(2026, 11, 15))
    renewal = (date(2026, 11, 15), date(2026, 12, 15))
    assert accounting_window([current, renewal], _at(date(2026, 11, 14))).start == (
        date(2026, 10, 15)
    )
    assert accounting_window([current, renewal], _at(date(2026, 11, 15), 0)).start == (
        date(2026, 11, 15)
    )


def test_without_a_current_cycle_the_months_chain_from_the_last_cycle_end() -> None:
    cycle = (date(2026, 1, 31), date(2026, 2, 28))
    assert accounting_window([cycle], _at(date(2026, 3, 1))) == AccountingWindow(
        date(2026, 2, 28), date(2026, 3, 28), "continuation"
    )
    # Months are added to the anchor, never chained day by day.
    assert accounting_window([cycle], _at(date(2026, 4, 2))) == AccountingWindow(
        date(2026, 3, 28), date(2026, 4, 28), "continuation"
    )
    assert accounting_window([cycle], _at(date(2026, 4, 27))).start == date(2026, 3, 28)


def test_a_late_renewal_falls_in_the_same_window_as_the_continuation() -> None:
    previous = (date(2026, 9, 15), date(2026, 10, 15))
    renewal = (date(2026, 10, 15), date(2026, 11, 15))
    at = _at(date(2026, 10, 18))
    before_grant = accounting_window([previous], at)
    after_grant = accounting_window([previous, renewal], at)
    assert before_grant.kind == "continuation" and after_grant.kind == "cycle"
    # Same start: the same opening key, so maintenance is never charged twice.
    assert before_grant.start == after_grant.start == date(2026, 10, 15)
    assert opening_source_key(before_grant, "rag") == opening_source_key(
        after_grant, "rag"
    )
    assert maintenance_effect_id(before_grant, "media") == maintenance_effect_id(
        after_grant, "media"
    )


def test_a_builder_that_never_had_a_cycle_uses_the_civil_month() -> None:
    window = accounting_window([], _at(date(2026, 12, 31), 23))
    assert window == AccountingWindow(date(2026, 12, 1), date(2027, 1, 1), "calendar")
    # 23:30 in Sao Paulo on Dec 31 is Jan 1 in UTC: the civil date decides.
    late = datetime(2027, 1, 1, 2, 30, tzinfo=timezone.utc)
    assert late.astimezone(CREDIT_TIMEZONE).date() == date(2026, 12, 31)
    assert accounting_window([], late).start == date(2026, 12, 1)


@settings(max_examples=300, deadline=None)
@given(
    anchor=st.dates(min_value=date(2016, 1, 1), max_value=date(2030, 12, 31)),
    offset_days=st.integers(min_value=0, max_value=800),
    minutes=st.integers(min_value=0, max_value=24 * 60 - 1),
)
def test_the_window_always_contains_its_instant(
    anchor: date, offset_days: int, minutes: int
) -> None:
    """Across month ends, leap years and the historical DST of Sao Paulo."""

    cycles = [(anchor, anchor + timedelta(days=30))]
    day = anchor + timedelta(days=offset_days)
    at = start_of_civil_day(day) + timedelta(minutes=minutes)
    civil = at.astimezone(CREDIT_TIMEZONE).date()
    window = accounting_window(cycles, at)
    assert window.start <= civil < window.end
    assert window.starts_at <= at < window.ends_at


def test_identities_are_stable_and_scoped() -> None:
    tenant, document, media, delivery = uuid4(), uuid4(), uuid4(), uuid4()
    window = AccountingWindow(date(2026, 10, 15), date(2026, 11, 15), "cycle")
    assert reservation_effect_id(tenant, "rag", document) == (
        f"rag:{tenant}:{document}:reserve"
    )
    assert reprocessing_effect_id(tenant, document, "job-1") == (
        f"rag:{tenant}:{document}:reprocess:job-1"
    )
    assert egress_effect_id(tenant, media, delivery) == (
        f"egress:{tenant}:{media}:{delivery}"
    )
    assert maintenance_effect_id(window, "rag") == "window:2026-10-15:rag-maintenance"
    assert opening_source_key(window, "media") == "window:2026-10-15:media:opening"
    with pytest.raises(ValueError):
        AccountingWindow(date(2026, 10, 15), date(2026, 10, 15), "cycle")
