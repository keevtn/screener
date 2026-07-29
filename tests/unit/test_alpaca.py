"""Alpaca intraday client: pagination, normalization, cache, degrade-graceful."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pipeline.marketdata.alpaca import AlpacaData, alpaca_configured


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")


class FakeResp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeHttp:
    """Two-page bars response, then latest-trade."""

    def __init__(self):
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        assert headers["APCA-API-KEY-ID"] == "test-key"
        if url.endswith("/bars"):
            if (params or {}).get("page_token"):
                return FakeResp(
                    {
                        "bars": [
                            {"t": "2026-07-16T09:01:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 20}
                        ],
                        "next_page_token": None,
                    }
                )
            return FakeResp(
                {
                    "bars": [
                        {"t": "2026-07-16T09:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 10}
                    ],
                    "next_page_token": "tok",
                }
            )
        if url.endswith("/stocks/trades/latest"):
            return FakeResp(
                {
                    "trades": {
                        "AAPL": {"p": 100.5, "t": "2026-07-16T09:03:00Z"},
                        "MSFT": {"p": None},
                    }
                }
            )
        if url.endswith("/trades/latest"):
            return FakeResp({"trade": {"p": 123.45, "t": "2026-07-16T09:02:00Z"}})
        raise AssertionError(url)


def test_configured_requires_both_keys(monkeypatch):
    assert alpaca_configured() is True
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    assert alpaca_configured() is False


def test_minute_bars_paginate_and_normalize():
    http = FakeHttp()
    bars = AlpacaData(http).minute_bars("aapl", start=datetime(2026, 7, 16, 8, 0, tzinfo=UTC))
    assert [b["time"] for b in bars] == ["2026-07-16T09:00:00Z", "2026-07-16T09:01:00Z"]
    assert bars[0] == {"time": "2026-07-16T09:00:00Z", "open": 1, "high": 1, "low": 1,
                       "close": 1, "volume": 10}
    # ticker uppercased into the URL; page 2 requested with the token
    assert "/stocks/AAPL/bars" in http.calls[0][0]
    assert http.calls[1][1].get("page_token") == "tok"


def test_latest_trade():
    assert AlpacaData(FakeHttp()).latest_trade("AAPL") == {
        "price": 123.45,
        "time": "2026-07-16T09:02:00Z",
    }


def test_latest_trades_batch_skips_priceless():
    out = AlpacaData(FakeHttp()).latest_trades(["aapl", "MSFT"])
    assert out == {"AAPL": {"price": 100.5, "time": "2026-07-16T09:03:00Z"}}


def test_latest_trades_empty_and_down():
    assert AlpacaData(FakeHttp()).latest_trades([]) == {}
    assert AlpacaData(DownHttp()).latest_trades(["AAPL"]) == {}


def test_cache_merge_and_serve(tmp_path):
    http = FakeHttp()
    d = AlpacaData(http, cache_dir=tmp_path)
    first = d.cached_minute_bars("AAPL", lookback_hours=100000)  # window covers the fakes
    assert len(first) == 2 and (tmp_path / "AAPL.parquet").exists()
    # second call re-fetches the tail and dedupes -> still 2 unique bars
    second = d.cached_minute_bars("AAPL", lookback_hours=100000)
    assert len(second) == 2


class DownHttp:
    def get(self, *a, **k):
        raise ConnectionError("down")


def test_cache_served_when_vendor_down(tmp_path):
    # seed the cache with a working client, then go dark
    AlpacaData(FakeHttp(), cache_dir=tmp_path).cached_minute_bars("AAPL", lookback_hours=100000)
    stale = AlpacaData(DownHttp(), cache_dir=tmp_path).cached_minute_bars(
        "AAPL", lookback_hours=100000
    )
    assert len(stale) == 2  # degrade to cache, never raise
