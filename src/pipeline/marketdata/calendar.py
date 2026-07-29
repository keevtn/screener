"""Trading-day calendar derived from cached benchmark (SPY) bars.

docs/ROADMAP.md task 0.4 / prediction-contract section 2. A day with a SPY bar is
a trading day (I10) — this avoids an exchange-calendar dependency. Holidays and
weekends simply have no bar and are therefore not trading days.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime

import pandas as pd


class CalendarRangeError(RuntimeError):
    """Requested a trading day beyond the calendar's known (cached) range."""


def _as_date(value: date | datetime | pd.Timestamp | str) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


class TradingCalendar:
    """Ordered set of trading dates with next/after helpers."""

    def __init__(self, days: Iterable[date | datetime | pd.Timestamp | str]) -> None:
        self._days: list[date] = sorted({_as_date(d) for d in days})
        self._pos: dict[date, int] = {d: i for i, d in enumerate(self._days)}

    @classmethod
    def from_bars(cls, bars: pd.DataFrame) -> TradingCalendar:
        """Build from a bars DataFrame with a 'date' column (e.g. SPY bars)."""
        return cls(bars["date"].tolist())

    def __len__(self) -> int:
        return len(self._days)

    @property
    def first(self) -> date:
        return self._days[0]

    @property
    def last(self) -> date:
        return self._days[-1]

    def is_trading_day(self, d: date | datetime | pd.Timestamp | str) -> bool:
        return _as_date(d) in self._pos

    def on_or_after(self, d: date | datetime | pd.Timestamp | str) -> date:
        """First trading day >= d."""
        target = _as_date(d)
        if target > self.last:
            raise CalendarRangeError(f"{target} is after the calendar end {self.last}")
        for day in self._days:
            if day >= target:
                return day
        raise CalendarRangeError(f"no trading day on or after {target}")

    def prev_trading_day(self, d: date | datetime | pd.Timestamp | str) -> date:
        """Last trading day strictly before d (premarket window anchor)."""
        target = _as_date(d)
        if target <= self.first:
            raise CalendarRangeError(f"no known trading day before {target} (start {self.first})")
        if target in self._pos:
            return self._days[self._pos[target] - 1]
        for day in reversed(self._days):
            if day < target:
                return day
        raise CalendarRangeError(f"no trading day before {target}")

    def next_trading_day(self, d: date | datetime | pd.Timestamp | str) -> date:
        """First trading day strictly after d."""
        target = _as_date(d)
        if target >= self.last:
            raise CalendarRangeError(f"no known trading day after {target} (end {self.last})")
        # If d itself is a trading day, step from its position; else use on_or_after.
        if target in self._pos:
            return self._days[self._pos[target] + 1]
        return self.on_or_after(target)

    def trading_days_after(self, d: date | datetime | pd.Timestamp | str, n: int) -> list[date]:
        """The next ``n`` trading days strictly after d (raises if run off the end)."""
        if n < 0:
            raise ValueError("n must be non-negative")
        out: list[date] = []
        cursor = _as_date(d)
        for _ in range(n):
            cursor = self.next_trading_day(cursor)
            out.append(cursor)
        return out
