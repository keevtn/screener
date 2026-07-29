"""Task 0.1/0.5 gate test: the prediction contract's worked examples (A-E), encoded.

If these numbers and docs/prediction-contract-v1.md ever disagree, one is wrong
on purpose (contract §6). θ=0.02, T=3, baseline C0 ticker=100.00, SPY=500.00.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd
from pytest import approx

from pipeline.grade import Grader, grade_prediction
from pipeline.marketdata import TradingCalendar

# C0 = Wed Mar 12 2025; horizon closes C1,C2,C3 = Thu 13, Fri 14, Mon 17.
C0, C1, C2, C3 = date(2025, 3, 12), date(2025, 3, 13), date(2025, 3, 14), date(2025, 3, 17)
ISSUED_DURING_HOURS = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)  # Wed 14:00 ET


@dataclass(frozen=True)
class Pred:
    ticker: str
    direction: str
    threshold: float
    horizon_trading_days: int
    issued_at: datetime


def _bars(closes: dict[date, float]) -> pd.DataFrame:
    rows = sorted(closes.items())
    return pd.DataFrame(
        {"date": [pd.Timestamp(d) for d, _ in rows], "adj_close": [p for _, p in rows]}
    )


def _provider(make_provider, ticker_closes, spy_closes, ticker="TEST"):
    return make_provider({ticker: _bars(ticker_closes), "SPY": _bars(spy_closes)})


# --- Example D: clock start ---------------------------------------------------


def test_example_d_clock_start():
    cal = TradingCalendar([d.date() for d in pd.bdate_range("2025-03-01", "2025-03-25")])
    grader = Grader(_DummyProvider())
    # Wed 14:00 ET (during hours) -> same-day close.
    assert grader.clock_start_date(datetime(2025, 3, 12, 18, 0, tzinfo=UTC), cal) == date(
        2025, 3, 12
    )
    # Wed 20:30 ET (after hours) -> next trading day.
    assert grader.clock_start_date(datetime(2025, 3, 13, 0, 30, tzinfo=UTC), cal) == date(
        2025, 3, 13
    )
    # Saturday -> next trading day (Mon Mar 17).
    assert grader.clock_start_date(datetime(2025, 3, 15, 15, 0, tzinfo=UTC), cal) == date(
        2025, 3, 17
    )
    # Horizon closes for the during-hours case.
    assert cal.trading_days_after(date(2025, 3, 12), 3) == [C1, C2, C3]


class _DummyProvider:
    benchmark = "SPY"


# --- Examples A, B, C, E: outcomes -------------------------------------------


def test_example_a_bullish_correct_day2(make_provider):
    ticker = {C0: 100.0, C1: 101.0, C2: 103.5, C3: 104.0}
    spy = {C0: 500.0, C1: 500.0, C2: 505.0, C3: 506.0}
    pred = Pred("TEST", "bullish", 0.02, 3, ISSUED_DURING_HOURS)
    res = grade_prediction(pred, _provider(make_provider, ticker, spy))
    assert res is not None
    assert res.outcome == "correct"
    assert res.realized_adjusted_return == approx(0.025)
    assert res.resolving_close == C2  # C3 never evaluated


def test_example_b_bullish_incorrect(make_provider):
    ticker = {C0: 100.0, C1: 99.5, C2: 97.0, C3: 100.0}
    spy = {C0: 500.0, C1: 502.0, C2: 500.0, C3: 500.0}
    pred = Pred("TEST", "bullish", 0.02, 3, ISSUED_DURING_HOURS)
    res = grade_prediction(pred, _provider(make_provider, ticker, spy))
    assert res.outcome == "incorrect"
    assert res.realized_adjusted_return == approx(-0.03)


def test_example_c_bullish_expired(make_provider):
    ticker = {C0: 100.0, C1: 101.0, C2: 99.8, C3: 102.0}
    spy = {C0: 500.0, C1: 502.0, C2: 503.0, C3: 504.0}
    pred = Pred("TEST", "bullish", 0.02, 3, ISSUED_DURING_HOURS)
    res = grade_prediction(pred, _provider(make_provider, ticker, spy))
    assert res.outcome == "expired"
    assert res.realized_adjusted_return == approx(0.012)  # r at C3
    assert res.resolving_close == C3


def test_example_e_bearish_sign_flip(make_provider):
    # Same bars as B; direction bearish -> C2's -3.0% crosses -theta -> correct.
    ticker = {C0: 100.0, C1: 99.5, C2: 97.0, C3: 100.0}
    spy = {C0: 500.0, C1: 502.0, C2: 500.0, C3: 500.0}
    pred = Pred("TEST", "bearish", 0.02, 3, ISSUED_DURING_HOURS)
    res = grade_prediction(pred, _provider(make_provider, ticker, spy))
    assert res.outcome == "correct"
    assert res.realized_adjusted_return == approx(-0.03)
