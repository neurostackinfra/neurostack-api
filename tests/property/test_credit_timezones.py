"""Calendar properties of credit expiry in ``America/Sao_Paulo`` (Group 2).

Generated ``paid_at``/``cycle_end`` dates (month ends, leap years) and
instants around the DST transitions São Paulo had until 2019 (nonexistent
midnights, ambiguous 23:00 hours) prove: persisted instants are UTC, a lot
is usable exactly while the São Paulo civil date is before its expiry day,
and six calendar months never truncate or round the date.
"""

from __future__ import annotations

import calendar
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from hypothesis import example, given, settings
from hypothesis import strategies as st

from src.services.credit_ledger import (
    CREDIT_TIMEZONE,
    LotState,
    add_calendar_months,
    civil_date,
    monthly_lot_expires_at,
    start_of_civil_day,
    topup_lot_expires_at,
    topup_lot_expires_on,
)

# 2000-2040 covers every São Paulo DST rule in the tz database and beyond.
DATES = st.dates(min_value=date(2000, 1, 1), max_value=date(2040, 12, 31))
MONTH_ENDS = st.builds(
    lambda year, month, back: date(year, month, calendar.monthrange(year, month)[1])
    - timedelta(days=back),
    st.integers(2000, 2040),
    st.integers(1, 12),
    st.integers(0, 3),
)
LOCAL_INSTANTS = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2040, 12, 31),
    timezones=st.just(CREDIT_TIMEZONE),
    allow_imaginary=True,
)


def _lot(expires_on: date) -> LotState:
    return LotState(
        id=uuid4(),
        lot_kind="topup",
        paid_at=expires_on - timedelta(days=183),
        expires_at=start_of_civil_day(expires_on),
        granted_mc=1_000,
        grant_effect_id="topup:x:grant",
        sums=Counter({"grant": 1_000}),
    )


@settings(max_examples=400, deadline=None)
@given(DATES)
@example(date(2018, 11, 4))  # DST began at 00:00: midnight did not exist
@example(date(2019, 2, 17))  # DST ended at 00:00: 23:00 of the 16th repeated
@example(date(2016, 2, 21))
def test_a_civil_day_starts_at_its_first_utc_instant(day: date) -> None:
    instant = start_of_civil_day(day)
    assert instant.tzinfo is timezone.utc and instant.utcoffset() == timedelta(0)
    assert civil_date(instant) == day
    assert civil_date(instant - timedelta(microseconds=1)) == day - timedelta(days=1)
    assert instant.minute == 0 and instant.second == 0 and instant.microsecond == 0


@settings(max_examples=400, deadline=None)
@given(st.one_of(DATES, MONTH_ENDS))
@example(date(2027, 8, 31))  # -> 2028-02-29 (leap)
@example(date(2026, 8, 31))  # -> 2027-02-28
@example(date(2026, 8, 29))  # -> 2027-02-28 (clamped)
@example(date(2027, 12, 31))  # year carry
def test_six_calendar_months_clamp_and_never_truncate(paid_at: date) -> None:
    expires_on = topup_lot_expires_on(paid_at)
    year, month_index = divmod(paid_at.year * 12 + paid_at.month - 1 + 6, 12)
    assert (expires_on.year, expires_on.month) == (year, month_index + 1)
    last = calendar.monthrange(expires_on.year, expires_on.month)[1]
    assert expires_on.day == min(paid_at.day, last)
    assert 181 <= (expires_on - paid_at).days <= 184
    assert add_calendar_months(paid_at, 6) == expires_on
    expires_at = topup_lot_expires_at(paid_at)
    assert expires_at == start_of_civil_day(expires_on)
    assert expires_at.utcoffset() == timedelta(0)


@settings(max_examples=600, deadline=None)
@given(LOCAL_INSTANTS, st.one_of(DATES, MONTH_ENDS))
@example(datetime(2018, 11, 4, 0, 30, tzinfo=CREDIT_TIMEZONE), date(2018, 11, 4))
@example(
    datetime(2019, 2, 16, 23, 30, tzinfo=CREDIT_TIMEZONE, fold=0), date(2019, 2, 17)
)
@example(
    datetime(2019, 2, 16, 23, 30, tzinfo=CREDIT_TIMEZONE, fold=1), date(2019, 2, 17)
)
@example(datetime(2019, 2, 17, 0, 0, tzinfo=CREDIT_TIMEZONE), date(2019, 2, 17))
def test_a_lot_is_usable_exactly_before_its_expiry_day(
    local: datetime, expires_on: date
) -> None:
    instant = local.astimezone(timezone.utc)
    lot = _lot(expires_on)
    assert lot.expired(instant) == (civil_date(instant) >= expires_on)
    assert (lot.available_mc(instant) == 0) == lot.expired(instant)


@settings(max_examples=300, deadline=None)
@given(DATES)
def test_the_monthly_allowance_ends_when_cycle_end_begins(cycle_end: date) -> None:
    expires_at = monthly_lot_expires_at(cycle_end)
    assert expires_at == start_of_civil_day(cycle_end)
    last_moment = expires_at - timedelta(microseconds=1)
    assert civil_date(last_moment) == cycle_end - timedelta(days=1)
    lot = _lot(cycle_end)
    assert not lot.expired(last_moment) and lot.expired(expires_at)
