"""Intraday bars: granularity fallback chain, TTL cache, extended-hours flags."""

from __future__ import annotations

import pandas as pd
import pytest

from pipeline.marketdata.intraday import intraday_bars


def _frame(times_et: list[str]) -> pd.DataFrame:
    """A yfinance-shaped frame with ET tz-aware index."""
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz="America/New_York") for t in times_et])
    n = len(idx)
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1000] * n,
        },
        index=idx,
    )


def test_1m_first_then_extended_flags(tmp_path):
    calls: list[tuple[str, str]] = []

    def dl(ticker, interval, period):
        calls.append((interval, period))
        return _frame(["2026-07-14 08:00", "2026-07-14 10:00", "2026-07-14 16:30"])

    r = intraday_bars("aapl", "1d", cache_dir=tmp_path, downloader=dl)
    assert r["available"] and r["interval"] == "1m" and r["ticker"] == "AAPL"
    assert calls == [("1m", "1d")]  # first chain entry succeeded — no fallback calls
    # pre-market (08:00) and after-hours (16:30) flagged; regular session not.
    assert [b["extended"] for b in r["bars"]] == [True, False, True]
    # ET-shifted epochs: 10:00 ET wall-clock renders as 10:00 on a UTC-epoch chart.
    ten = r["bars"][1]
    assert pd.Timestamp(ten["time"], unit="s").hour == 10


def test_granularity_fallback_to_5m(tmp_path):
    def dl(ticker, interval, period):
        if interval == "1m":
            return pd.DataFrame()  # yfinance has no 1m for this ticker/window
        return _frame(["2026-07-14 10:00"])

    r = intraday_bars("MSFT", "1d", cache_dir=tmp_path, downloader=dl)
    assert r["available"] and r["interval"] == "5m"


def test_ttl_cache_skips_refetch(tmp_path):
    calls = []

    def dl(ticker, interval, period):
        calls.append(interval)
        return _frame(["2026-07-14 10:00"])

    r1 = intraday_bars("NVDA", "1d", cache_dir=tmp_path, downloader=dl)
    r2 = intraday_bars("NVDA", "1d", cache_dir=tmp_path, downloader=dl)
    assert len(calls) == 1  # second call served from parquet within TTL
    assert r2["available"] and r2["interval"] == "1m"
    assert r1["bars"][0]["close"] == r2["bars"][0]["close"]


def test_unavailable_is_negative_cached(tmp_path):
    calls = []

    def dl(ticker, interval, period):
        calls.append(interval)
        return pd.DataFrame()

    r1 = intraday_bars("NOPE", "1w", cache_dir=tmp_path, downloader=dl)
    n_after_first = len(calls)
    r2 = intraday_bars("NOPE", "1w", cache_dir=tmp_path, downloader=dl)
    assert r1["available"] is False and r1["bars"] == []
    assert r2["available"] is False
    assert len(calls) == n_after_first  # negative-cached: no second chain walk
    assert n_after_first == 2  # 1w chain = 5m, 15m


def test_unknown_window_rejected(tmp_path):
    with pytest.raises(ValueError):
        intraday_bars("AAPL", "3mo", cache_dir=tmp_path, downloader=lambda *a: pd.DataFrame())
