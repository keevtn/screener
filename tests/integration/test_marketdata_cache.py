"""Task 0.4 gate test: parquet cache serves repeats with zero fetch; adj_close used."""

from __future__ import annotations

from datetime import date

import pandas as pd

from pipeline.marketdata import BAR_COLUMNS, MarketDataProvider


class CountingDownloader:
    """Fake downloader: synthetic bars + an invocation counter (stands in for HTTP)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, ticker: str, start: date, end: date) -> pd.DataFrame:
        self.calls += 1
        idx = pd.bdate_range(start, end)
        # adj_close deliberately differs from close so "used in returns" is provable.
        return pd.DataFrame(
            {
                "date": idx,
                "open": range(len(idx)),
                "high": range(len(idx)),
                "low": range(len(idx)),
                "adj_close": [100.0 + i for i in range(len(idx))],
                "volume": [1000] * len(idx),
            }
        )


def test_second_identical_call_performs_zero_fetch(tmp_path):
    dl = CountingDownloader()
    provider = MarketDataProvider(tmp_path / "bars", downloader=dl)

    first = provider.get_daily_bars("AAPL", date(2025, 3, 3), date(2025, 3, 14))
    assert dl.calls == 1
    assert list(first.columns) == BAR_COLUMNS
    assert not first.empty

    # Identical request: served from cache, no new fetch (I10 cache guarantee).
    second = provider.get_daily_bars("AAPL", date(2025, 3, 3), date(2025, 3, 14))
    assert dl.calls == 1
    pd.testing.assert_frame_equal(first, second)

    # A narrower sub-range is still covered -> no fetch.
    provider.get_daily_bars("AAPL", date(2025, 3, 5), date(2025, 3, 10))
    assert dl.calls == 1


def test_cache_persists_across_provider_instances(tmp_path):
    dl1 = CountingDownloader()
    MarketDataProvider(tmp_path / "bars", downloader=dl1).get_daily_bars(
        "SPY", date(2025, 3, 3), date(2025, 3, 14)
    )
    assert dl1.calls == 1

    dl2 = CountingDownloader()
    fresh = MarketDataProvider(tmp_path / "bars", downloader=dl2)
    bars = fresh.get_daily_bars("SPY", date(2025, 3, 3), date(2025, 3, 14))
    assert dl2.calls == 0  # reads the parquet written by the first instance
    assert bars["adj_close"].iloc[0] == 100.0


def test_cached_quote_reads_cache_only(tmp_path):
    dl = CountingDownloader()
    provider = MarketDataProvider(tmp_path / "bars", downloader=dl)
    provider.get_daily_bars("AAPL", date(2025, 3, 3), date(2025, 3, 7))  # Mon..Fri -> 100..104
    assert dl.calls == 1

    q = provider.cached_quote("AAPL")
    assert dl.calls == 1  # quote never fetches (safe at request time)
    assert q is not None
    assert q["last"] == 104.0
    assert q["pct_change"] == 0.97  # (104-103)/103*100
    assert q["vol_over_avg"] == 1.0  # constant volume
    assert q["as_of"] == "2025-03-07"

    assert provider.cached_quote("UNCACHED") is None  # absent -> UI renders "—"


def test_widening_range_triggers_one_more_fetch(tmp_path):
    dl = CountingDownloader()
    provider = MarketDataProvider(tmp_path / "bars", downloader=dl)
    provider.get_daily_bars("AAPL", date(2025, 3, 3), date(2025, 3, 7))
    assert dl.calls == 1
    # Range extends past covered end -> exactly one refetch of the union.
    provider.get_daily_bars("AAPL", date(2025, 3, 3), date(2025, 3, 21))
    assert dl.calls == 2
    provider.get_daily_bars("AAPL", date(2025, 3, 10), date(2025, 3, 14))
    assert dl.calls == 2  # now covered
