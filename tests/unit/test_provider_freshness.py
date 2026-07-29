"""Provider cache freshness: future-end requests must not freeze the cache."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from pipeline.marketdata.provider import MarketDataProvider


def _bars(*days):
    return pd.DataFrame(
        {"date": [pd.Timestamp(d) for d in days], "open": 1.0, "high": 1.0,
         "low": 1.0, "adj_close": 1.0, "volume": 100}
    )


def test_future_end_request_does_not_poison_coverage(tmp_path):
    calls = []
    today = date(2026, 7, 16)

    def dl(ticker, start, end):
        calls.append((start, end))
        # vendor only has bars through "yesterday" on the first call
        return _bars("2026-07-14", "2026-07-15")

    import pipeline.marketdata.provider as prov
    orig = prov._market_today
    prov._market_today = lambda: today
    try:
        p = MarketDataProvider(cache_dir=tmp_path, downloader=dl)
        # marking-style request 40 days into the future
        p.get_daily_bars("AAPL", date(2026, 7, 1), date(2026, 8, 25))
        assert len(calls) == 1
        # a later same-day request inside TTL: cache hit (no hammering)
        p.get_daily_bars("AAPL", date(2026, 7, 1), date(2026, 8, 25))
        assert len(calls) == 1
        # NEXT DAY (new session close exists): must refetch, not freeze
        prov._market_today = lambda: today + timedelta(days=1)
        p.get_daily_bars("AAPL", date(2026, 7, 1), date(2026, 8, 25))
        assert len(calls) == 2, "cache stayed frozen after the market day rolled"
        # purely-historical request: served from coverage, no fetch
        p.get_daily_bars("AAPL", date(2026, 7, 10), date(2026, 7, 14))
        assert len(calls) == 2
    finally:
        prov._market_today = orig


def test_retry_once_then_succeed(tmp_path):
    calls = []

    def flaky(ticker, start, end):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("dead socket")
        return _bars("2026-07-14", "2026-07-15")

    p = MarketDataProvider(cache_dir=tmp_path, downloader=flaky)
    df = p.get_daily_bars("AAPL", date(2026, 7, 14), date(2026, 7, 15))
    assert len(calls) == 2 and len(df) == 2  # first failure retried transparently


def test_double_failure_degrades_to_cached_slice(tmp_path):
    good_calls = []

    def good(ticker, start, end):
        good_calls.append(1)
        return _bars("2026-07-14", "2026-07-15")

    p = MarketDataProvider(cache_dir=tmp_path, downloader=good)
    p.get_daily_bars("AAPL", date(2026, 7, 14), date(2026, 7, 15))  # seed cache

    def dead(ticker, start, end):
        raise ConnectionError("down")

    p2 = MarketDataProvider(cache_dir=tmp_path, downloader=dead)
    # force a fetch attempt (request beyond coverage) -> double failure -> stale slice
    df = p2.get_daily_bars("AAPL", date(2026, 7, 14), date(2026, 8, 30))
    assert len(df) == 2  # served from cache, sweep survives
