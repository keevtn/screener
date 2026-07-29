"""Grader v0 (docs/ROADMAP.md task 0.5 / docs/prediction-contract-v1.md).

Grades an open prediction by walking the T horizon closes after the clock-start
close C0, computing the market-adjusted return r_k = ticker_ret_k - spy_ret_k on
adjusted close (I10), and resolving on the first band crossing (contract §4).

Constants (T, θ, benchmark, close_time, exchange_tz) live on the prediction row
and — for close_time/exchange_tz — default to the contract until the versioned
config loader lands (task 4.3). No look-ahead: C0 is always strictly after
issued_at (I12), guaranteed by the calendar clock-start rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from pipeline.common.timeutil import utcnow
from pipeline.marketdata import CalendarRangeError, MarketDataProvider, TradingCalendar

DEFAULT_EXCHANGE_TZ = "America/New_York"
DEFAULT_CLOSE_TIME = "16:00"


class _PredictionLike(Protocol):
    ticker: str
    direction: str
    threshold: float
    horizon_trading_days: int
    issued_at: datetime


@dataclass(frozen=True)
class GradeResult:
    """Outcome of grading one prediction (None outcome return means 'stay open')."""

    outcome: str  # correct | incorrect | expired
    realized_adjusted_return: float | None
    resolving_close: date  # the crossing close, or CT for expired
    graded_at: datetime


def _parse_close_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


class Grader:
    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        exchange_tz: str = DEFAULT_EXCHANGE_TZ,
        close_time: str = DEFAULT_CLOSE_TIME,
    ) -> None:
        self.provider = provider
        self.benchmark = provider.benchmark
        self._tz = ZoneInfo(exchange_tz)
        self._close = _parse_close_time(close_time)

    # --- clock start (contract §2) ------------------------------------------
    def clock_start_date(self, issued_at: datetime, calendar: TradingCalendar) -> date:
        """C0 trading date: same day if issued on a trading day strictly before the
        close, else the next trading day (after-hours/weekend/holiday)."""
        local = issued_at.astimezone(self._tz)
        d = local.date()
        before_close = local.time() < self._close
        if calendar.is_trading_day(d) and before_close:
            return d
        return calendar.next_trading_day(d)

    # --- grading (contract §4/§5) -------------------------------------------
    def grade(self, pred: _PredictionLike) -> GradeResult | None:
        """Grade one open prediction. Returns None if the horizon has not fully
        elapsed / data is not yet available (the prediction stays open)."""
        issued: datetime = pred.issued_at
        win_start = issued.date() - timedelta(days=10)
        # Generous forward window: enough calendar days to contain T trading closes.
        win_end = issued.date() + timedelta(days=pred.horizon_trading_days * 4 + 21)

        spy = self.provider.get_benchmark_bars(win_start, win_end)
        if spy.empty:
            return None
        calendar = TradingCalendar.from_bars(spy)

        try:
            c0 = self.clock_start_date(issued, calendar)
            horizon = calendar.trading_days_after(c0, pred.horizon_trading_days)
        except CalendarRangeError:
            return None  # not enough future trading days yet -> stay open
        ct = horizon[-1]

        spy_close = _closes_by_date(spy)
        # SPY defines the calendar, but guard against a gap between C0 and CT.
        if c0 not in spy_close or any(d not in spy_close for d in horizon):
            return None

        ticker = self.provider.get_daily_bars(pred.ticker, win_start, win_end)
        tk_close = _closes_by_date(ticker)

        base_m = spy_close[c0]
        base_t = tk_close.get(c0)
        if base_t is None:
            # No baseline bar for the ticker at C0 (halt/delisting at clock start).
            # ROADMAP-NOTE: boring resolution — expired with NULL realized return.
            return GradeResult("expired", None, ct, utcnow())

        sign = 1.0 if pred.direction == "bullish" else -1.0
        r: float | None = None
        for d in horizon:
            if d not in tk_close:  # halt: no bar this close, skip but it still counts
                continue
            r = (tk_close[d] / base_t - 1.0) - (spy_close[d] / base_m - 1.0)
            if sign * r >= pred.threshold:
                return GradeResult("correct", r, d, utcnow())
            if sign * r <= -pred.threshold:
                return GradeResult("incorrect", r, d, utcnow())
        return GradeResult("expired", r, ct, utcnow())


def _closes_by_date(bars: pd.DataFrame) -> dict[date, float]:
    if bars.empty:
        return {}
    return {ts.date(): float(px) for ts, px in zip(bars["date"], bars["adj_close"], strict=True)}


def grade_prediction(
    pred: _PredictionLike,
    provider: MarketDataProvider,
    *,
    exchange_tz: str = DEFAULT_EXCHANGE_TZ,
    close_time: str = DEFAULT_CLOSE_TIME,
) -> GradeResult | None:
    """Convenience wrapper matching the roadmap signature grade_prediction(pred)."""
    return Grader(provider, exchange_tz=exchange_tz, close_time=close_time).grade(pred)


def apply_grade(session, pred, result: GradeResult) -> None:
    """Write a GradeResult onto a Prediction ORM row (only the I4 grader fields)."""
    pred.status = "graded"
    pred.outcome = result.outcome
    pred.realized_adjusted_return = result.realized_adjusted_return
    pred.graded_at = result.graded_at
    pred.resolving_close = result.resolving_close
    session.commit()
