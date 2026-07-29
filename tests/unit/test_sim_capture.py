"""Minute-path capture merge logic (no network — injected data client)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd

from pipeline.sim.capture import capture_trade_paths

T0 = datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


class _FakeData:
    def __init__(self, bars):
        self._bars = bars
        self.calls = []

    def minute_bars(self, ticker, *, start, end):
        self.calls.append((ticker, start, end))
        return self._bars


def _trade(ticker, entered, exited):
    return SimpleNamespace(ticker=ticker, entered_at=entered, exited_at=exited)


def _bars(n):
    return [
        {"time": f"2026-07-20T14:{i:02d}:00Z", "open": 10, "high": 11, "low": 9,
         "close": 10, "volume": 100}
        for i in range(n)
    ]


def test_capture_writes_and_dedupes(tmp_path):
    data = _FakeData(_bars(3))
    trades = [_trade("AAA", T0, T0 + timedelta(minutes=30))]
    got = capture_trade_paths(data, trades, cache_dir=tmp_path)
    assert got == {"AAA": 3}
    df = pd.read_parquet(tmp_path / "AAA.parquet")
    assert len(df) == 3
    assert {"time", "open", "high", "low", "close", "volume"}.issubset(df.columns)
    # the fetch window unions [entry, exit] with a pad
    _, start, end = data.calls[0]
    assert start < T0 and end > T0 + timedelta(minutes=30)
    # re-capturing the same bars merges + dedupes on time -> still 3 rows
    capture_trade_paths(data, trades, cache_dir=tmp_path)
    assert len(pd.read_parquet(tmp_path / "AAA.parquet")) == 3


def test_capture_empty_feed_writes_nothing(tmp_path):
    got = capture_trade_paths(
        _FakeData([]), [_trade("THIN", T0, T0 + timedelta(minutes=10))], cache_dir=tmp_path
    )
    assert got == {"THIN": 0}  # honest: IEX had nothing, not an error
    assert not (tmp_path / "THIN.parquet").exists()


def test_capture_fetch_error_isolated(tmp_path):
    class _Boom:
        def minute_bars(self, ticker, *, start, end):
            raise RuntimeError("boom")

    got = capture_trade_paths(
        _Boom(), [_trade("X", T0, T0 + timedelta(minutes=5))], cache_dir=tmp_path
    )
    assert got == {"X": 0}  # one ticker's failure -> 0, never raises
