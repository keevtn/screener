"""Task 0.5 gate test: golden price CSVs drive correct/incorrect/expired + edges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
from pytest import approx

from pipeline.grade import grade_prediction

PRICES = Path(__file__).resolve().parents[1] / "fixtures" / "prices"
ISSUED = datetime(2025, 3, 12, 18, 0, tzinfo=UTC)  # Wed during hours -> C0 = Mar 12
C2 = date(2025, 3, 14)
C3 = date(2025, 3, 17)


@dataclass(frozen=True)
class Pred:
    ticker: str
    direction: str
    threshold: float
    horizon_trading_days: int
    issued_at: datetime


def _csv(name: str) -> pd.DataFrame:
    return pd.read_csv(PRICES / name, parse_dates=["date"])


def _provider(make_provider, scenario: str):
    return make_provider({"TICK": _csv(f"{scenario}_TICK.csv"), "SPY": _csv(f"{scenario}_SPY.csv")})


def test_correct_crosses_up_on_day2(make_provider):
    pred = Pred("TICK", "bullish", 0.02, 3, ISSUED)
    res = grade_prediction(pred, _provider(make_provider, "correct"))
    assert res.outcome == "correct"
    # Hand-computed SPY adjustment: (103.5/100 - 1) - (505/500 - 1) = 0.025.
    assert res.realized_adjusted_return == approx(0.025)
    assert res.resolving_close == C2


def test_incorrect_crosses_down_first(make_provider):
    pred = Pred("TICK", "bullish", 0.02, 3, ISSUED)
    res = grade_prediction(pred, _provider(make_provider, "incorrect"))
    assert res.outcome == "incorrect"
    assert res.realized_adjusted_return == approx(-0.03)


def test_expired_never_crosses(make_provider):
    pred = Pred("TICK", "bullish", 0.02, 3, ISSUED)
    res = grade_prediction(pred, _provider(make_provider, "expired"))
    assert res.outcome == "expired"
    assert res.realized_adjusted_return == approx(0.012)  # r at C3
    assert res.resolving_close == C3


def test_halt_day_skipped_but_still_counts(make_provider):
    # Ticker halted (no bar) on C1 and C2; only C3 has a bar, and it crosses.
    ticker = pd.DataFrame(
        {
            "date": [pd.Timestamp("2025-03-12"), pd.Timestamp("2025-03-17")],
            "adj_close": [100.0, 103.0],
        }
    )
    spy = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-03-12", "2025-03-13", "2025-03-14", "2025-03-17"]),
            "adj_close": [500.0, 500.0, 500.0, 500.0],
        }
    )
    pred = Pred("TICK", "bullish", 0.02, 3, ISSUED)
    res = grade_prediction(pred, make_provider({"TICK": ticker, "SPY": spy}))
    assert res.outcome == "correct"  # +3% adj at C3
    assert res.resolving_close == C3


def test_delisting_no_bars_expires_null(make_provider):
    # Ticker has no bar at C0 (nor anywhere in the horizon) -> expired, NULL return.
    ticker = pd.DataFrame({"date": pd.to_datetime([]), "adj_close": []})
    spy = pd.DataFrame(
        {
            "date": pd.to_datetime(["2025-03-12", "2025-03-13", "2025-03-14", "2025-03-17"]),
            "adj_close": [500.0, 500.0, 500.0, 500.0],
        }
    )
    pred = Pred("TICK", "bullish", 0.02, 3, ISSUED)
    res = grade_prediction(pred, make_provider({"TICK": ticker, "SPY": spy}))
    assert res.outcome == "expired"
    assert res.realized_adjusted_return is None


def test_horizon_not_elapsed_stays_open(make_provider):
    # SPY has C0 and C1 only; C2/C3 don't exist yet -> cannot grade -> stay open.
    ticker = pd.DataFrame(
        {"date": pd.to_datetime(["2025-03-12", "2025-03-13"]), "adj_close": [100.0, 101.0]}
    )
    spy = pd.DataFrame(
        {"date": pd.to_datetime(["2025-03-12", "2025-03-13"]), "adj_close": [500.0, 500.0]}
    )
    pred = Pred("TICK", "bullish", 0.02, 3, ISSUED)
    assert grade_prediction(pred, make_provider({"TICK": ticker, "SPY": spy})) is None
