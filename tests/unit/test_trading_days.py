"""Task 0.4 gate test: trading-day calendar — Friday + 2 = Tuesday; holidays skip."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from pipeline.marketdata import CalendarRangeError, TradingCalendar

# A plain two-week block of weekdays (no holidays).
FULL_WEEKS = [d.date() for d in pd.bdate_range("2025-03-03", "2025-03-14")]
# Same block with Monday 2025-03-10 removed to model a market holiday.
WITH_HOLIDAY = [d for d in FULL_WEEKS if d != date(2025, 3, 10)]


def test_friday_plus_two_trading_days_is_tuesday():
    cal = TradingCalendar(FULL_WEEKS)
    friday = date(2025, 3, 7)
    assert cal.trading_days_after(friday, 2) == [date(2025, 3, 10), date(2025, 3, 11)]
    assert cal.trading_days_after(friday, 2)[-1] == date(2025, 3, 11)  # Tuesday


def test_holiday_is_skipped():
    cal = TradingCalendar(WITH_HOLIDAY)
    friday = date(2025, 3, 7)
    # Monday Mar 10 is a holiday (no bar) -> next trading day is Tuesday Mar 11.
    assert cal.next_trading_day(friday) == date(2025, 3, 11)
    assert cal.trading_days_after(friday, 2) == [date(2025, 3, 11), date(2025, 3, 12)]
    assert not cal.is_trading_day(date(2025, 3, 10))
    assert not cal.is_trading_day(date(2025, 3, 8))  # Saturday


def test_on_or_after_and_weekend_rolls_forward():
    cal = TradingCalendar(FULL_WEEKS)
    # Saturday Mar 8 -> first trading day on/after is Monday Mar 10.
    assert cal.on_or_after(date(2025, 3, 8)) == date(2025, 3, 10)
    # A trading day maps to itself.
    assert cal.on_or_after(date(2025, 3, 10)) == date(2025, 3, 10)


def test_running_off_the_end_raises():
    cal = TradingCalendar(FULL_WEEKS)
    with pytest.raises(CalendarRangeError):
        cal.trading_days_after(date(2025, 3, 14), 1)  # last day; nothing after
