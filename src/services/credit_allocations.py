"""RAG and media reservation rules (neurostack-credit-system Group 4a).

Pure: no I/O.  :mod:`src.services.credit_inventory` applies these rules
under the builder's account lock; the property tests drive them directly.

Units and conversions (the PDF, DESIGN "ledger and metering").  RAG is
measured in characters (``len(content)`` / ``char_length``), one credit per
:data:`RAG_CHARACTERS_PER_CREDIT`; media storage in bytes, one credit per
:data:`MEDIA_BYTES_PER_CREDIT` (decimal MB); every media credit covered in a
window includes :data:`EGRESS_INCLUDED_BYTES_PER_CREDIT` bytes of downloads,
and the bytes served beyond cost one credit per
:data:`EGRESS_OVERAGE_BYTES_PER_CREDIT`.  Rounding happens ONCE, on the
aggregate of the window (:func:`aggregate_target_mc`), never per file, chunk
or download.

The reservation is a DEBIT (owner decision B, 2026-10-30).  Per accounting
window and category, the target is ``ceil(units x 1000 / unit)`` over all the
active content and only ``target - coverage`` is debited, where the coverage
is the largest target already recorded in that window (:func:`charge_delta`):
deleting content never lowers the coverage (no refund), and new content in
the same window reuses the coverage a deletion freed.  A debit beyond the
capacity becomes debt, as any debit.  Nothing extra is subtracted from
``available_trace_mc``: the reservation has already left the lots.

RAG reprocessing (deliverable 3): within a window, re-indexing up to 100 % of
the active characters is included; what exceeds it is added to the units of
the RAG target (:func:`rag_units`), so the excess is charged through the same
aggregate delta.

The accounting window (owner decision C) is a range of civil dates in
``America/Sao_Paulo``: a subscription cycle ``[cycle_start, cycle_end)``;
without a current monthly cycle, ``[last cycle_end, +1 month)`` and the
months after it (chained from that ``cycle_end``); the civil month for a
builder that never had a cycle.  A window is identified by its START date,
so a renewal granted late (``cycle_start`` = the last ``cycle_end``) falls in
the same window as the continuation and never pays its maintenance twice.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal
from uuid import UUID

from src.services.credit_ledger import (
    MILLICREDITS_PER_CREDIT,
    add_calendar_months,
    civil_date,
    start_of_civil_day,
)

Category = Literal["rag", "media"]
AllocationCategory = Literal["rag", "media", "egress"]
WindowKind = Literal["cycle", "continuation", "calendar"]

RAG_CHARACTERS_PER_CREDIT: Final = 5_000
MEDIA_BYTES_PER_CREDIT: Final = 20_000_000
EGRESS_INCLUDED_BYTES_PER_CREDIT: Final = 200_000_000
EGRESS_OVERAGE_BYTES_PER_CREDIT: Final = 100_000_000
# 100 % of the active characters may be reprocessed in a window for free.
RAG_REPROCESSING_INCLUDED_PERCENT: Final = 100

# The PDF's limits; they apply only after ``credit_enforcement_at``.
KNOWLEDGE_DOCUMENT_MAX_CHARACTERS: Final = 500_000
KNOWLEDGE_FILE_MAX_BYTES: Final = 20_000_000
KNOWLEDGE_BATCH_MAX_FILES: Final = 10
MEDIA_IMAGE_MAX_BYTES: Final = 5_000_000
MEDIA_PDF_MAX_BYTES: Final = 20_000_000
MEDIA_AUDIO_VIDEO_MAX_BYTES: Final = 16_000_000
# Per media category of ``media_assets`` (the five existing types: M4A and
# OGG-Opus are deferred, owner decision A, and keep ``main``'s
# ``media_type_unsupported``).
ENFORCED_MEDIA_MAX_BYTES: Final[dict[str, int]] = {
    "image": MEDIA_IMAGE_MAX_BYTES,
    "pdf": MEDIA_PDF_MAX_BYTES,
    "audio": MEDIA_AUDIO_VIDEO_MAX_BYTES,
    "video": MEDIA_AUDIO_VIDEO_MAX_BYTES,
}

_UNIT_PER_CREDIT: Final[dict[str, int]] = {
    "rag": RAG_CHARACTERS_PER_CREDIT,
    "media": MEDIA_BYTES_PER_CREDIT,
    "egress": EGRESS_OVERAGE_BYTES_PER_CREDIT,
}


def _non_negative(value: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def aggregate_target_mc(units: int, category: AllocationCategory) -> int:
    """``ceil(units x 1000 / unit)`` in integers: the one rounding of a
    category's aggregate (never per file or chunk)."""

    _non_negative(units, "units")
    unit = _UNIT_PER_CREDIT[category]
    return -(-units * MILLICREDITS_PER_CREDIT // unit)


def charge_delta(target_mc: int, coverage_mc: int) -> int:
    """What a target adds to a window's coverage: never negative (a smaller
    target -- deleted content -- refunds nothing)."""

    _non_negative(target_mc, "target_mc")
    _non_negative(coverage_mc, "coverage_mc")
    return max(target_mc - coverage_mc, 0)


def rag_units(active_characters: int, reprocessed_characters: int) -> int:
    """The RAG units a window's target counts: the active characters plus
    the reprocessing beyond the included share of them."""

    _non_negative(active_characters, "active_characters")
    _non_negative(reprocessed_characters, "reprocessed_characters")
    included = active_characters * RAG_REPROCESSING_INCLUDED_PERCENT // 100
    return active_characters + max(reprocessed_characters - included, 0)


def egress_allowance_bytes(media_coverage_mc: int) -> int:
    """Download bytes included by the media coverage of a window (200 MB
    per credit; the egress overage itself never adds allowance)."""

    _non_negative(media_coverage_mc, "media_coverage_mc")
    return (
        media_coverage_mc * EGRESS_INCLUDED_BYTES_PER_CREDIT // MILLICREDITS_PER_CREDIT
    )


def egress_target_mc(delivered_bytes: int, allowance_bytes: int) -> int:
    """The egress overage target of a window: the bytes served beyond the
    allowance, rounded once."""

    _non_negative(delivered_bytes, "delivered_bytes")
    _non_negative(allowance_bytes, "allowance_bytes")
    return aggregate_target_mc(max(delivered_bytes - allowance_bytes, 0), "egress")


# ---------------------------------------------------------------------------
# The accounting window.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountingWindow:
    """``[start, end)`` in civil dates of ``America/Sao_Paulo``."""

    start: date
    end: date
    kind: WindowKind

    def __post_init__(self) -> None:
        if not self.start < self.end:
            raise ValueError("an accounting window ends after it starts")

    @property
    def starts_at(self) -> datetime:
        """The first instant of the window (UTC)."""

        return start_of_civil_day(self.start)

    @property
    def ends_at(self) -> datetime:
        return start_of_civil_day(self.end)


def accounting_window(
    monthly_cycles: Iterable[tuple[date, date]], at: datetime
) -> AccountingWindow:
    """The window containing ``at`` (owner decision C).

    ``monthly_cycles`` are the builder's monthly lots' ``(cycle_start,
    cycle_end)``.  A cycle containing the civil date of ``at`` is the window
    (the latest start, should two overlap); otherwise the months chained from
    the latest ``cycle_end`` not after that date; otherwise the civil month.
    A cycle that has not started yet (an early renewal) never decides the
    window before its ``cycle_start``.
    """

    today = civil_date(at)
    cycles = sorted(set(monthly_cycles))
    current = [cycle for cycle in cycles if cycle[0] <= today < cycle[1]]
    if current:
        start, end = max(current)
        return AccountingWindow(start, end, "cycle")
    ended = [end for _start, end in cycles if end <= today]
    if ended:
        anchor = max(ended)
        months = 0
        while add_calendar_months(anchor, months + 1) <= today:
            months += 1
        start = add_calendar_months(anchor, months)
        return AccountingWindow(
            start, add_calendar_months(anchor, months + 1), "continuation"
        )
    first = today.replace(day=1)
    return AccountingWindow(first, add_calendar_months(first, 1), "calendar")


# ---------------------------------------------------------------------------
# Identities (stable: a retry of the same operation yields the same id).
# ---------------------------------------------------------------------------


def opening_source_key(window: AccountingWindow, category: Category) -> str:
    """The one opening (baseline or maintenance) of a window and category."""

    return f"window:{window.start.isoformat()}:{category}:opening"


def maintenance_effect_id(window: AccountingWindow, category: Category) -> str:
    """One maintenance effect per window and category (owner decision C:
    no snapshot version in it; the snapshot digest is on the allocation)."""

    return f"window:{window.start.isoformat()}:{category}-maintenance"


def reservation_effect_id(tenant_id: UUID, category: Category, object_id: UUID) -> str:
    """The reservation of one upload (a document, a batch, a media asset):
    an internal retry of the same upload reuses it."""

    return f"{category}:{tenant_id}:{object_id}:reserve"


def reprocessing_effect_id(tenant_id: UUID, object_id: UUID, job_id: str) -> str:
    """One RAG reprocessing job of one object: its retry reuses it."""

    return f"rag:{tenant_id}:{object_id}:reprocess:{job_id}"


def egress_effect_id(tenant_id: UUID, media_id: UUID, delivery_id: UUID) -> str:
    """One served download (``delivery_id`` is generated by the server for
    every request, owner decision F: every request counts)."""

    return f"egress:{tenant_id}:{media_id}:{delivery_id}"


def observational_maintenance_effect_id(
    scope_id: UUID, window: AccountingWindow, category: Category
) -> str:
    """The observational maintenance of a window (``scope_id``: the builder,
    or the tenant of a standalone tenant)."""

    return f"maintenance:{scope_id}:{window.start.isoformat()}:{category}"


__all__ = [
    "EGRESS_INCLUDED_BYTES_PER_CREDIT",
    "EGRESS_OVERAGE_BYTES_PER_CREDIT",
    "ENFORCED_MEDIA_MAX_BYTES",
    "KNOWLEDGE_BATCH_MAX_FILES",
    "KNOWLEDGE_DOCUMENT_MAX_CHARACTERS",
    "KNOWLEDGE_FILE_MAX_BYTES",
    "MEDIA_AUDIO_VIDEO_MAX_BYTES",
    "MEDIA_BYTES_PER_CREDIT",
    "MEDIA_IMAGE_MAX_BYTES",
    "MEDIA_PDF_MAX_BYTES",
    "RAG_CHARACTERS_PER_CREDIT",
    "RAG_REPROCESSING_INCLUDED_PERCENT",
    "AccountingWindow",
    "AllocationCategory",
    "Category",
    "accounting_window",
    "aggregate_target_mc",
    "charge_delta",
    "egress_allowance_bytes",
    "egress_effect_id",
    "egress_target_mc",
    "maintenance_effect_id",
    "observational_maintenance_effect_id",
    "opening_source_key",
    "rag_units",
    "reprocessing_effect_id",
    "reservation_effect_id",
]
