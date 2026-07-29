"""Intraday bars (1m/5m/15m) via yfinance with a TTL'd parquet cache.

The daily parquet cache can't chart a single session, so the ticker detail's
1D/1W views fetch REAL intraday bars here — on demand per viewed ticker (never
bulk, minding yfinance's tolerance), cached under ``data/bars_intraday`` with a
short TTL because intraday bars go stale within minutes during market hours.

Granularity is a fallback chain per window (yfinance limits 1m data to the last
~7-30 days and small periods): 1D tries 1m then 5m/15m; 1W tries 5m then 15m.
``prepost=True`` — yfinance INCLUDES pre-market and after-hours prints; each bar
carries an ``extended`` flag (outside 09:30–16:00 ET) so the chart can shade
them rather than pass them off as regular-session candles. When every interval
comes back empty the result is ``available: False`` — the UI falls back to the
honest last-close panel, never synthetic bars.

Bar ``time`` values are epoch seconds SHIFTED so a chart that renders epochs as
UTC displays Eastern wall-clock time (the standard lightweight-charts intraday
convention); the payload says ``times: "ET"`` explicitly.
"""

from __future__ import annotations

import json
import time as _time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd

# downloader(ticker, interval, period) -> raw yfinance-shaped DataFrame.
Downloader = Callable[[str, str, str], pd.DataFrame]

# (interval, period) fallback chains per window.
_CHAINS: dict[str, list[tuple[str, str]]] = {
    "1d": [("1m", "1d"), ("5m", "1d"), ("15m", "1d")],
    "1w": [("5m", "5d"), ("15m", "5d")],
}
_TTL_SECONDS = 300  # intraday bars stale fast; refetch after 5 min
_ET = "America/New_York"


def _yf_intraday(ticker: str, interval: str, period: str) -> pd.DataFrame:
    import yfinance as yf

    raw = yf.download(
        ticker,
        interval=interval,
        period=period,
        prepost=True,  # include pre/after-hours; bars are flagged `extended`
        auto_adjust=False,
        progress=False,
        actions=False,
        timeout=15,  # a dead socket must not hang the API worker (cf. provider.py)
    )
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw = raw.rename(columns={c: str(c).strip().lower() for c in raw.columns})
    return raw


def _to_bars(raw: pd.DataFrame) -> list[dict[str, Any]]:
    """yfinance frame -> chart bars: ET-shifted epoch time + extended flag."""
    idx = pd.to_datetime(raw.index)
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    et = idx.tz_convert(_ET)
    # Epochs computed from the NAIVE ET wall-clock (displays as ET on the chart).
    shifted = et.tz_localize(None)
    minutes = et.hour * 60 + et.minute
    extended = (minutes < 9 * 60 + 30) | (minutes >= 16 * 60)
    bars: list[dict[str, Any]] = []
    for i, ts in enumerate(shifted):
        o, h, lo, c = (raw["open"].iloc[i], raw["high"].iloc[i],
                       raw["low"].iloc[i], raw["close"].iloc[i])
        if pd.isna(c) or pd.isna(o):
            continue
        v = raw["volume"].iloc[i] if "volume" in raw.columns else None
        bars.append(
            {
                "time": int(ts.timestamp()),
                "open": round(float(o), 4),
                "high": round(float(h), 4),
                "low": round(float(lo), 4),
                "close": round(float(c), 4),
                "volume": int(v) if v is not None and not pd.isna(v) else None,
                "extended": bool(extended[i]),
            }
        )
    return bars


def intraday_bars(
    ticker: str,
    window: str = "1d",
    *,
    cache_dir: str | Path = "data/bars_intraday",
    ttl: float = _TTL_SECONDS,
    downloader: Downloader | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Intraday bars for one ticker+window, cached. available=False when no
    interval in the chain returns data (UI then falls back to last close)."""
    ticker = ticker.strip().upper()
    if window not in _CHAINS:
        raise ValueError(f"unknown intraday window {window!r} (use one of {sorted(_CHAINS)})")
    fetch = downloader or _yf_intraday
    now = now if now is not None else _time.time()

    cdir = Path(cache_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    cache = cdir / f"{ticker}_{window}.parquet"
    meta_path = cdir / f"{ticker}_{window}.meta.json"

    # Fresh cache hit (mtime within TTL) — no network.
    if cache.exists() and meta_path.exists() and now - cache.stat().st_mtime < ttl:
        try:
            df = pd.read_parquet(cache)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return {
                "ticker": ticker,
                "window": window,
                "available": bool(len(df)),
                "interval": meta.get("interval"),
                "prepost": True,
                "times": "ET",
                "bars": df.to_dict("records"),
            }
        except Exception:  # noqa: BLE001 — corrupt cache: refetch below
            pass

    for interval, period in _CHAINS[window]:
        try:
            raw = fetch(ticker, interval, period)
        except Exception:  # noqa: BLE001 — network/yfinance hiccup: try coarser
            continue
        if raw is None or raw.empty:
            continue
        bars = _to_bars(raw)
        if not bars:
            continue
        pd.DataFrame(bars).to_parquet(cache, index=False)
        meta_path.write_text(json.dumps({"interval": interval}), encoding="utf-8")
        return {
            "ticker": ticker,
            "window": window,
            "available": True,
            "interval": interval,
            "prepost": True,
            "times": "ET",
            "bars": bars,
        }

    # Negative cache: remember "nothing available" for one TTL so repeated views
    # of a bar-less ticker don't retry the whole chain on every request.
    pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "extended"]).to_parquet(
        cache, index=False
    )
    meta_path.write_text(json.dumps({"interval": None}), encoding="utf-8")
    return {
        "ticker": ticker,
        "window": window,
        "available": False,
        "interval": None,
        "prepost": True,
        "times": "ET",
        "bars": [],
    }
