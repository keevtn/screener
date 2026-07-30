"""Alpaca market-data client — intraday/premarket minute bars (Phase 1 of the sim).

Free paper-tier setup: bars come from the IEX feed (real trades, ~2% of
consolidated volume — sparse on thin small-caps, which the sim's cost model
compensates for), and Alpaca bars include pre/post-market trades natively, which
is exactly the coverage the overnight-catalyst study needs and yfinance lacks.

Degrade-graceful like everything else: no keys in the env -> `alpaca_configured()`
is False and callers fall back (the intraday panel keeps its client bucketing;
the sim simply can't run). Minute bars are cached to parquet per ticker under
data/bars_intraday/ so repeated marks/backtests don't re-fetch.

The paper BROKER side (orders/fills) is deliberately NOT here — that lands with
the sim ledger so the human-gate semantics arrive with it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from pipeline.common.timeutil import utcnow

log = logging.getLogger("pipeline.marketdata.alpaca")

DATA_URL = "https://data.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
_CACHE_DIR = Path("data/bars_intraday")


def alpaca_keys() -> tuple[str, str] | None:
    """(key, secret) from the env, or None. Supports both common secret names."""
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_SECRET_KEY")
    return (key, secret) if key and secret else None


def alpaca_configured() -> bool:
    return alpaca_keys() is not None


class AlpacaData:
    """Minimal REST client for Alpaca's stock data API (+ paper account ping).

    ``http`` is injectable for tests (anything with .get(url, headers=, params=,
    timeout=) returning a requests-style response).
    """

    def __init__(self, http: Any | None = None, *, cache_dir: Path | None = None) -> None:
        keys = alpaca_keys()
        if keys is None:
            raise RuntimeError("Alpaca keys missing (ALPACA_API_KEY / ALPACA_API_SECRET)")
        self._headers = {"APCA-API-KEY-ID": keys[0], "APCA-API-SECRET-KEY": keys[1]}
        self._http = http or requests.Session()
        self._cache_dir = cache_dir or _CACHE_DIR

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self._http.get(url, headers=self._headers, params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json()

    def account(self) -> dict[str, Any]:
        """Paper account state (status, buying power) — the connectivity check."""
        return self._get(f"{PAPER_URL}/v2/account")

    def clock(self) -> dict[str, Any] | None:
        """The Alpaca trading clock — the calendar authority for the daily sim
        loop. Returns {is_open, timestamp, next_open, next_close} with the three
        timestamps parsed to aware datetimes (Alpaca returns ET-offset ISO, which
        natively encodes holidays, half-days, and DST — no local calendar needed).
        Fail-soft None so the loop waits/retries rather than trading blind."""
        try:
            body = self._get(f"{PAPER_URL}/v2/clock")
        except Exception as exc:  # noqa: BLE001 — clock outage -> caller idles, never trades blind
            log.warning("alpaca clock fetch failed (%s) — treating as unknown", type(exc).__name__)
            return None

        def _dt(v: Any) -> datetime | None:
            if not v:
                return None
            try:
                return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                return None

        return {
            "is_open": bool(body.get("is_open")),
            "timestamp": _dt(body.get("timestamp")),
            "next_open": _dt(body.get("next_open")),
            "next_close": _dt(body.get("next_close")),
        }

    def minute_bars(
        self,
        ticker: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: str = "1Min",
        feed: str = "iex",
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Minute bars (pre/post-market included), oldest first, paginated.

        Each bar: {time (ISO UTC), open, high, low, close, volume}.
        """
        ticker = ticker.upper()
        start = start or (utcnow() - timedelta(hours=48))
        params: dict[str, Any] = {
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "limit": 10000,
            "feed": feed,
            "adjustment": "raw",
        }
        if end is not None:
            params["end"] = end.isoformat().replace("+00:00", "Z")
        out: list[dict[str, Any]] = []
        for _ in range(max_pages):
            body = self._get(f"{DATA_URL}/v2/stocks/{ticker}/bars", params)
            for b in body.get("bars") or []:
                out.append(
                    {
                        "time": b["t"],
                        "open": b["o"],
                        "high": b["h"],
                        "low": b["l"],
                        "close": b["c"],
                        "volume": b["v"],
                    }
                )
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out

    def daily_bars(self, ticker: str, *, lookback_days: int = 45) -> list[dict[str, Any]]:
        """Daily OHLC bars for the last ``lookback_days`` calendar days, oldest
        first — the vol_stop exit's ATR source. Reuses the minute_bars pagination
        with a 1Day timeframe. Empty list on any failure (fail-soft: no vol -> the
        exit policy falls back to the horizon backstop)."""
        try:
            return self.minute_bars(
                ticker,
                start=utcnow() - timedelta(days=lookback_days),
                timeframe="1Day",
                feed="iex",
            )
        except Exception:  # noqa: BLE001 — vol is optional; never crash a sweep
            return []

    def latest_trade(self, ticker: str) -> dict[str, Any] | None:
        """Most recent trade print {price, time} (IEX feed), or None."""
        try:
            body = self._get(
                f"{DATA_URL}/v2/stocks/{ticker.upper()}/trades/latest", {"feed": "iex"}
            )
            t = body.get("trade") or {}
            return {"price": t.get("p"), "time": t.get("t")} if t.get("p") is not None else None
        except Exception:  # noqa: BLE001 — quote absence must never break a caller
            return None

    def latest_trades(self, tickers: list[str]) -> dict[str, dict[str, Any]]:
        """Batched latest trade prints for many tickers in ONE request — the live
        screener's quote source. {TICKER: {price, time}}; absent tickers (no IEX
        print) are simply missing. Empty dict on any failure (fail-soft)."""
        if not tickers:
            return {}
        symbols = ",".join(sorted({t.upper() for t in tickers}))
        try:
            body = self._get(
                f"{DATA_URL}/v2/stocks/trades/latest", {"symbols": symbols, "feed": "iex"}
            )
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict[str, Any]] = {}
        for sym, t in (body.get("trades") or {}).items():
            if t and t.get("p") is not None:
                out[sym.upper()] = {"price": t["p"], "time": t.get("t")}
        return out

    # --- parquet cache -----------------------------------------------------
    def _cache_path(self, ticker: str) -> Path:
        return self._cache_dir / f"{ticker.upper().replace('/', '-')}.parquet"

    def cached_minute_bars(
        self, ticker: str, *, lookback_hours: int = 48
    ) -> list[dict[str, Any]]:
        """Cache-backed minute bars for the last `lookback_hours`.

        Fetches only the tail beyond the newest cached bar, merges + dedupes on
        timestamp, persists, and serves the window. On any fetch failure the
        cached slice (possibly stale) is served — degrade, don't break.
        """
        ticker = ticker.upper()
        path = self._cache_path(ticker)
        cached = pd.DataFrame()
        if path.exists():
            try:
                cached = pd.read_parquet(path)
            except Exception:  # noqa: BLE001 — corrupt cache -> refetch
                cached = pd.DataFrame()

        window_start = utcnow() - timedelta(hours=lookback_hours)
        fetch_start = window_start
        if not cached.empty:
            newest = pd.Timestamp(cached["time"].max())
            if newest.tzinfo is None:
                newest = newest.tz_localize("UTC")
            fetch_start = max(window_start, newest.to_pydatetime())

        try:
            fresh = self.minute_bars(ticker, start=fetch_start)
        except Exception as exc:  # noqa: BLE001
            log.info("alpaca fetch failed for %s (%s) — serving cache", ticker, type(exc).__name__)
            fresh = []

        if fresh:
            merged = pd.concat([cached, pd.DataFrame(fresh)], ignore_index=True)
            merged = merged.drop_duplicates(subset="time", keep="last").sort_values("time")
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            merged.to_parquet(path, index=False)
            cached = merged

        if cached.empty:
            return []
        cutoff = window_start.isoformat().replace("+00:00", "Z")
        view = cached[cached["time"] >= cutoff]
        return view.to_dict("records")
